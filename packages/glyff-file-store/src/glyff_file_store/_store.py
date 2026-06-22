from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypedDict

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


class _FileTransaction(Transaction):
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FileExecution(Execution):
    def __init__(
        self,
        call_stack: list[str],
        serializer: Serializer,
        append_entry: Callable[[LogEntry], Any],
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
    A file-based SessionStore that logs execution events to JSON.

    Each START / COMPLETE / FAIL / delete mutation writes the log immediately,
    so completed descendant calls survive a later root interruption or process
    crash. The entire log is still loaded into memory and rewritten per event;
    for very large or high-throughput sessions prefer an append- or
    database-backed store.
    """

    def __init__(self, client: FileClient, serializer: Serializer):
        self._client = client
        self._serializer = serializer
        self._lock = asyncio.Lock()
        self._executions_path = Path("executions.json")
        # Canonical, ordered list of durable log entries — the in-memory mirror
        # of executions.json. Source of truth for both serialization and
        # per-key lookup; ``_latest_index`` is a positional pointer into this
        # list.
        self._log_entries: list[LogEntry] = []
        # key → index of the latest log entry that defines the current state for
        # that key. Indices are stable because we only append to ``_log_entries``
        # except when pruning rewrites the list and rebuilds the index.
        self._latest_index: dict[str, int] = {}
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
    # Durable log mutation
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_entries(entries: list[LogEntry]) -> bytes:
        return json.dumps(
            entries, indent=2, sort_keys=True, separators=JSON_SEPARATORS
        ).encode(DEFAULT_ENCODING)

    async def _add_log_entry(self, entry: LogEntry):
        async with self._lock:
            next_entries = [*self._log_entries, entry]
            await self._client.write(
                self._executions_path,
                self._serialize_entries(next_entries),
            )
            start_index = len(self._log_entries)
            self._log_entries = next_entries
            self._index_new_entries(start_index=start_index)

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    async def begin_transaction(self) -> Transaction:
        return _FileTransaction()

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
        async with self._lock:
            idx = self._latest_index.get(key)
            if idx is None:
                return None
            entry = self._log_entries[idx]
            status = _EVENT_TYPE_TO_STATUS[entry["event_type"]]
            persistable_result = entry["result"]
            error = entry["error"]

        result: Any | None = None
        if status == ExecutionStatus.COMPLETED and persistable_result is not None:
            serialized_bytes = json.dumps(
                persistable_result, sort_keys=True, separators=JSON_SEPARATORS
            ).encode(DEFAULT_ENCODING)
            result = await self._serializer.deserialize(serialized_bytes, return_type)
        elif status == ExecutionStatus.FAILED:
            error = error or ""
        return ExecutionRecord(status=status, result=result, error=error)

    async def get_descendants(self, execution_id: ExecutionId) -> list[ExecutionId]:
        prefix = self._id_to_key(execution_id) + "/"
        async with self._lock:
            keys: dict[str, list[str]] = {}
            for entry in self._log_entries:
                key = self._callstack_to_key(entry["call_stack"])
                if key.startswith(prefix):
                    keys.setdefault(key, entry["call_stack"])
        return [self._callstack_to_id(call_stack) for call_stack in keys.values()]

    async def delete_executions(self, execution_ids: Iterable[ExecutionId]) -> None:
        keys = {execution_id_to_path(eid) for eid in execution_ids}
        if not keys:
            return
        async with self._lock:
            next_entries = [
                entry
                for entry in self._log_entries
                if self._callstack_to_key(entry["call_stack"]) not in keys
            ]
            if len(next_entries) == len(self._log_entries):
                return
            await self._client.write(
                self._executions_path,
                self._serialize_entries(next_entries),
            )
            self._log_entries = next_entries
            self._latest_index.clear()
            self._index_new_entries(start_index=0)
