from __future__ import annotations

import asyncio
from abc import ABC
from datetime import datetime, timezone
from typing import Awaitable, Callable, TypedDict

from glyff.interfaces import Execution, Serializer, SessionStore, Transaction
from glyff.models import ExecutionId, ExecutionStatus

from ..file_client import FileClient

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
        store: BaseFileSessionStore,
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
        import json

        serialized_bytes = self._serializer.serialize(value, return_type)
        persistable_result = json.loads(serialized_bytes)
        entry = self._create_log_entry(
            ExecutionStatus.COMPLETED, result=persistable_result
        )
        await self._store._add_log_entry(entry)

    async def fail(self, error: str) -> None:
        entry = self._create_log_entry(ExecutionStatus.FAILED, error=error)
        await self._store._add_log_entry(entry)


class BaseFileSessionStore(SessionStore, ABC):
    """Base class for file-based session stores."""

    def __init__(self, client: FileClient, serializer: Serializer):
        self._client = client
        self._serializer = serializer
        self._lock = asyncio.Lock()

    def _id_to_callstack(self, execution_id: ExecutionId) -> list[str]:
        """Converts an ExecutionId to a call stack list (outermost → innermost)."""
        frames: list[str] = []
        current: ExecutionId | None = execution_id
        while current is not None:
            frames.append(f"{current.name}#{current.sequence}:{current.args_hash}")
            current = current.parent_id
        frames.reverse()
        return frames

    def _id_to_key(self, execution_id: ExecutionId) -> str:
        """Converts an ExecutionId to a stable, unique string key."""
        parent_path = (
            f"{self._id_to_key(execution_id.parent_id)}/"
            if execution_id.parent_id
            else ""
        )
        return f"{parent_path}{execution_id.name}#{execution_id.sequence}:{execution_id.args_hash}"

    @staticmethod
    def _callstack_to_key(call_stack: list[str]) -> str:
        """Converts a call stack list back to the stable string key."""
        return "/".join(call_stack)

    async def begin_transaction(self) -> Transaction:
        return _FileTransaction(
            self._client,
            on_commit=self._on_transaction_commit,
            on_rollback=self._on_transaction_rollback,
        )

    async def _on_transaction_commit(self) -> None:
        """Hook invoked after a successful commit. Override to apply
        deferred in-memory state changes."""

    async def _on_transaction_rollback(self) -> None:
        """Hook invoked after a rollback. Override to discard deferred
        in-memory state changes."""

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

    async def _add_log_entry(self, entry: LogEntry) -> None:
        """Adds a log entry to the staging area for the current transaction."""
        raise NotImplementedError
