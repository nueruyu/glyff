from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TypedDict

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRecord,
    ExecutionStatus,
    Serializer,
    SessionStore,
    Transaction,
)
from glyff.serialization.constants import DEFAULT_ENCODING, JSON_SEPARATORS
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._file_client import FileClient

logger = logging.getLogger(__name__)


class LogEntry(TypedDict):
    timestamp: str
    event_type: str  # "start", "complete", "fail"
    call_stack: list[str]
    result: object | None
    error: str | None


_STATUS_TO_EVENT_TYPE = {
    ExecutionStatus.STARTED: "start",
    ExecutionStatus.COMPLETED: "complete",
    ExecutionStatus.FAILED: "fail",
}
_EVENT_TYPE_TO_STATUS = {v: k for k, v in _STATUS_TO_EVENT_TYPE.items()}


class _StagingBuffer:
    """A single transaction's pending log entries and deletes."""

    __slots__ = ("entries", "delete_keys")

    def __init__(self) -> None:
        self.entries: list[LogEntry] = []
        self.delete_keys: set[str] = set()

    def clear(self) -> None:
        self.entries.clear()
        self.delete_keys.clear()


class _FileTransaction(Transaction):
    def __init__(self, store: JsonFileSessionStore):
        self._store = store
        self._closed = False
        self._lock = asyncio.Lock()
        # Isolate this transaction's staging from any concurrent transaction.
        self._token = store.begin_staging()

    async def commit(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                await self._store._commit_current()
            finally:
                self._store.end_staging(self._token)

    async def rollback(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                await self._store._rollback_current()
            finally:
                self._store.end_staging(self._token)


class _FileExecution(Execution):
    def __init__(
        self,
        call_stack: list[str],
        serializer: Serializer,
        append_entry: Callable[[LogEntry], Awaitable[None]],
    ):
        self._call_stack = call_stack
        self._serializer = serializer
        self._append_entry = append_entry

    def _create_log_entry(
        self,
        status: ExecutionStatus,
        result: object | None = None,
        error: str | None = None,
    ) -> LogEntry:
        return LogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=_STATUS_TO_EVENT_TYPE[status],
            call_stack=self._call_stack,
            result=result,
            error=error,
        )

    async def complete(self, value: object, return_type: type) -> None:
        serialized_bytes = await self._serializer.serialize(value, return_type)
        persistable_result = json.loads(serialized_bytes)
        entry = self._create_log_entry(
            ExecutionStatus.COMPLETED, result=persistable_result
        )
        await self._append_entry(entry)

    async def fail(self, error: str) -> None:
        entry = self._create_log_entry(ExecutionStatus.FAILED, error=error)
        await self._append_entry(entry)


class JsonFileSessionStore(SessionStore):
    """
    A file-based SessionStore that logs events to a pretty-printed JSON file.
    The entire log is loaded into memory at construction time and rewritten
    atomically on each commit. Suitable for sessions whose log fits in memory;
    for very large or high-throughput sessions prefer a database-backed store.

    This is a human-readable debug backend. Each transaction stages into its
    own buffer (tracked per asyncio task via a ``ContextVar``), and commits are
    serialized so the whole-file rewrite stays consistent under concurrent
    transactions — so it is parallel-safe, but each commit still rewrites the
    whole file (O(n) per commit). For large or high-throughput durable
    workloads prefer ``SQLiteSessionStore``.
    """

    def __init__(self, client: FileClient, serializer: Serializer):
        self._client = client
        self._serializer = serializer
        self._lock = asyncio.Lock()
        self._executions_path = Path("executions.json")
        # Canonical, ordered list of committed log entries — the in-memory
        # mirror of executions.json. Source of truth for both serialization
        # and per-key lookup; ``_latest_index`` is a positional pointer into
        # this list.
        self._log_entries: list[LogEntry] = []
        # key → index of the latest log entry that defines the current
        # state for that key. Indices are stable because we only append
        # to ``_log_entries`` except when a delete rebuilds the list.
        self._latest_index: dict[str, int] = {}
        # Per-transaction staging, tracked per task so concurrent transactions
        # (parallel gather branches) stay isolated. Writes require an active
        # transaction; read helpers use the empty ambient buffer outside one.
        self._ambient = _StagingBuffer()
        self._current: contextvars.ContextVar[_StagingBuffer | None] = (
            contextvars.ContextVar("json_store_staging", default=None)
        )
        self._load_executions()

    # ------------------------------------------------------------------
    # Id / call-stack helpers
    # ------------------------------------------------------------------

    def _id_to_callstack(self, execution_id: ExecutionId) -> list[str]:
        """Convert an ExecutionId to a call stack list (outermost → innermost)."""
        return execution_id_to_path(execution_id).split("/")

    def _id_to_key(self, execution_id: ExecutionId) -> str:
        """Convert an ExecutionId to a stable, unique string key."""
        return execution_id_to_path(execution_id)

    @staticmethod
    def _callstack_to_key(call_stack: list[str]) -> str:
        return "/".join(call_stack)

    @staticmethod
    def _callstack_to_id(call_stack: list[str]) -> ExecutionId:
        """Rebuild the full ExecutionId chain from a persisted call stack."""
        return path_to_execution_id("/".join(call_stack))

    # ------------------------------------------------------------------
    # Loading and in-memory state
    # ------------------------------------------------------------------

    def _load_executions(self) -> None:
        abs_path = self._client.resolve(self._executions_path)
        if not abs_path.exists():
            return

        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return
        try:
            entries: list[LogEntry] = json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning(
                "Could not parse executions log %s; ignoring corrupted file: %s",
                abs_path,
                e,
            )
            return

        self._log_entries = entries
        self._index_new_entries(start_index=0)

    def _index_new_entries(self, start_index: int) -> None:
        """Update ``_latest_index`` for log entries at positions
        ``[start_index, len(_log_entries))``. Each known event type
        replaces any prior index for its key; entries with unrecognized
        event types are ignored."""
        for i in range(start_index, len(self._log_entries)):
            entry = self._log_entries[i]
            if entry["event_type"] not in _EVENT_TYPE_TO_STATUS:
                continue
            key = self._callstack_to_key(entry["call_stack"])
            self._latest_index[key] = i

    # ------------------------------------------------------------------
    # Per-transaction staging and commit
    # ------------------------------------------------------------------

    def _staging(self) -> _StagingBuffer:
        """Current transaction staging, or an empty ambient read buffer."""
        return self._current.get() or self._ambient

    def _require_staging(self) -> _StagingBuffer:
        staging = self._current.get()
        if staging is None:
            raise RuntimeError(
                "JsonFileSessionStore write attempted outside a transaction."
            )
        return staging

    def begin_staging(self) -> contextvars.Token:
        return self._current.set(_StagingBuffer())

    def end_staging(self, token: contextvars.Token) -> None:
        try:
            self._current.reset(token)
        except (ValueError, LookupError):
            pass

    async def _add_log_entry(self, entry: LogEntry) -> None:
        staging = self._require_staging()
        async with self._lock:
            staging.entries.append(entry)

    async def _commit_current(self) -> None:
        staging = self._require_staging()
        async with self._lock:
            if not staging.entries and not staging.delete_keys:
                return

            if staging.delete_keys:
                deleted = staging.delete_keys
                merged = [
                    e
                    for e in (self._log_entries + staging.entries)
                    if self._callstack_to_key(e["call_stack"]) not in deleted
                ]
                content = self._serialize_entries(merged)
                # Write to disk first; only advance in-memory state on success.
                await self._write_all(content)
                self._log_entries = merged
                self._latest_index.clear()
                self._index_new_entries(start_index=0)
            else:
                start_index = len(self._log_entries)
                merged = self._log_entries + staging.entries
                content = self._serialize_entries(merged)
                await self._write_all(content)
                self._log_entries = merged
                self._index_new_entries(start_index=start_index)

            staging.clear()

    async def _rollback_current(self) -> None:
        staging = self._require_staging()
        async with self._lock:
            staging.clear()

    @staticmethod
    def _serialize_entries(entries: list[LogEntry]) -> bytes:
        return json.dumps(
            entries, indent=2, sort_keys=True, separators=JSON_SEPARATORS
        ).encode(DEFAULT_ENCODING)

    async def _write_all(self, content: bytes) -> None:
        """Rewrite the whole executions file atomically via the file client."""
        await self._client.stage_write(self._executions_path, content)
        await self._client.commit_staged()

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    async def begin_transaction(self) -> Transaction:
        return _FileTransaction(self)

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        record = await self.get_execution_record(execution_id, type(None))
        if record is None:
            entry = LogEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type=_STATUS_TO_EVENT_TYPE[ExecutionStatus.STARTED],
                call_stack=self._id_to_callstack(execution_id),
                result=None,
                error=None,
            )
            await self._add_log_entry(entry)
        return _FileExecution(
            call_stack=self._id_to_callstack(execution_id),
            serializer=self._serializer,
            append_entry=self._add_log_entry,
        )

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        key = self._id_to_key(execution_id)
        idx = self._latest_index.get(key)
        if idx is None:
            return None

        entry = self._log_entries[idx]
        status = _EVENT_TYPE_TO_STATUS[entry["event_type"]]
        result: Any | None = None
        error: str | None = None
        if status == ExecutionStatus.COMPLETED:
            persistable_result = entry["result"]
            if persistable_result is not None:
                serialized_bytes = json.dumps(
                    persistable_result, sort_keys=True, separators=JSON_SEPARATORS
                ).encode(DEFAULT_ENCODING)
                result = await self._serializer.deserialize(
                    serialized_bytes, return_type
                )
        elif status == ExecutionStatus.FAILED:
            error = entry["error"] or ""
        return ExecutionRecord(status=status, result=result, error=error)

    async def get_descendants(self, execution_id: ExecutionId) -> list[ExecutionId]:
        prefix = self._id_to_key(execution_id) + "/"
        async with self._lock:
            staging = self._staging()
            all_entries = self._log_entries + staging.entries
            deleted = staging.delete_keys
            keys: dict[str, list[str]] = {}
            for entry in all_entries:
                key = self._callstack_to_key(entry["call_stack"])
                if key.startswith(prefix) and key not in deleted:
                    keys.setdefault(key, entry["call_stack"])
        return [self._callstack_to_id(call_stack) for call_stack in keys.values()]

    async def delete_executions(self, execution_ids: Iterable[ExecutionId]) -> None:
        keys = {execution_id_to_path(eid) for eid in execution_ids}
        if not keys:
            return
        staging = self._require_staging()
        async with self._lock:
            staging.delete_keys.update(keys)
