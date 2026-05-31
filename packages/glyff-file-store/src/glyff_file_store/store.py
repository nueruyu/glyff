from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TypedDict

from glyff.interfaces import Execution, Serializer, SessionStore, Transaction
from glyff.models import ExecutionId, ExecutionRecord, ExecutionStatus

from .file_client import FileClient

logger = logging.getLogger(__name__)

PostHook = Callable[[], Awaitable[None]]


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
    def __init__(
        self,
        client: FileClient,
        on_commit: PostHook | None = None,
        on_rollback: PostHook | None = None,
    ):
        self._client = client
        self._on_commit = on_commit
        self._on_rollback = on_rollback

    async def commit(self) -> None:
        await self._client.commit_staged()
        if self._on_commit is not None:
            await self._on_commit()

    async def rollback(self) -> None:
        await self._client.clear_staged()
        if self._on_rollback is not None:
            await self._on_rollback()


class _FileExecution(Execution):
    def __init__(
        self,
        store: JsonFileSessionStore,
        execution_id: ExecutionId,
        serializer: Serializer,
    ):
        self._store = store
        self._id = execution_id
        self._serializer = serializer

    def _create_log_entry(
        self,
        status: ExecutionStatus,
        result: object | None = None,
        error: str | None = None,
    ) -> LogEntry:
        return LogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=_STATUS_TO_EVENT_TYPE[status],
            call_stack=self._store._id_to_callstack(self._id),
            result=result,
            error=error,
        )

    async def complete(self, value: object, return_type: type) -> None:
        serialized_bytes = self._serializer.serialize(value, return_type)
        persistable_result = json.loads(serialized_bytes)
        entry = self._create_log_entry(
            ExecutionStatus.COMPLETED, result=persistable_result
        )
        await self._store._add_log_entry(entry)

    async def fail(self, error: str) -> None:
        entry = self._create_log_entry(ExecutionStatus.FAILED, error=error)
        await self._store._add_log_entry(entry)


class JsonFileSessionStore(SessionStore):
    """
    A file-based SessionStore that logs events to a pretty-printed JSON file.
    The entire log is loaded into memory at construction time and rewritten
    atomically on each commit. Suitable for sessions whose log fits in memory;
    for very large or high-throughput sessions prefer a database-backed store.
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
        # to ``_log_entries``, never remove.
        self._latest_index: dict[str, int] = {}
        # Entries staged by the current transaction, not yet committed.
        self._staged_log_entries: list[LogEntry] = []
        self._load_executions()

    # ------------------------------------------------------------------
    # Id / call-stack helpers
    # ------------------------------------------------------------------

    def _id_to_callstack(self, execution_id: ExecutionId) -> list[str]:
        """Convert an ExecutionId to a call stack list (outermost → innermost)."""
        frames: list[str] = []
        current: ExecutionId | None = execution_id
        while current is not None:
            frames.append(f"{current.name}#{current.sequence}:{current.args_hash}")
            current = current.parent_id
        frames.reverse()
        return frames

    def _id_to_key(self, execution_id: ExecutionId) -> str:
        """Convert an ExecutionId to a stable, unique string key."""
        parent_path = (
            f"{self._id_to_key(execution_id.parent_id)}/"
            if execution_id.parent_id
            else ""
        )
        return f"{parent_path}{execution_id.name}#{execution_id.sequence}:{execution_id.args_hash}"

    @staticmethod
    def _callstack_to_key(call_stack: list[str]) -> str:
        return "/".join(call_stack)

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
    # Staging and commit
    # ------------------------------------------------------------------

    async def _on_write(self) -> bytes:
        """Produce the new on-disk content from in-memory state. Called by
        FileClient at commit time."""
        async with self._lock:
            all_entries = self._log_entries + self._staged_log_entries
        return json.dumps(all_entries, indent=2, sort_keys=True).encode("utf-8")

    async def _on_transaction_commit(self) -> None:
        async with self._lock:
            start_index = len(self._log_entries)
            self._log_entries.extend(self._staged_log_entries)
            self._index_new_entries(start_index=start_index)
            self._staged_log_entries.clear()

    async def _on_transaction_rollback(self) -> None:
        async with self._lock:
            self._staged_log_entries.clear()

    async def _add_log_entry(self, entry: LogEntry):
        # Append under the store lock, then call stage_write outside the
        # store lock to avoid a lock-ordering inversion against _on_write
        # (which acquires the store lock during commit_staged).
        async with self._lock:
            must_stage = not self._staged_log_entries
            self._staged_log_entries.append(entry)

        if must_stage:
            await self._client.stage_write(
                self._executions_path, self._on_write
            )

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    async def begin_transaction(self) -> Transaction:
        return _FileTransaction(
            self._client,
            on_commit=self._on_transaction_commit,
            on_rollback=self._on_transaction_rollback,
        )

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
        return _FileExecution(self, execution_id, self._serializer)

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
                    persistable_result, sort_keys=True
                ).encode("utf-8")
                result = self._serializer.deserialize(serialized_bytes, return_type)
        elif status == ExecutionStatus.FAILED:
            error = entry["error"] or ""
        return ExecutionRecord(status=status, result=result, error=error)
