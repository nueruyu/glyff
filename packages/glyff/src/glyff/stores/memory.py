from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from ..interfaces import Execution, Serializer, SessionStore, Transaction
from ..models import ExecutionId, ExecutionRecord, ExecutionStatus
from .memory_client import MemoryClient

_KEY_PREFIX = "execution::"
_PARTS = ("status", "result", "error")


def _make_key(path: str, part: str) -> str:
    return f"{_KEY_PREFIX}{path}::{part}"


def _key_to_path(key: str) -> str | None:
    """Extract the path body from a full key, or None if not an execution key."""
    if not key.startswith(_KEY_PREFIX):
        return None
    body, _, _ = key[len(_KEY_PREFIX) :].rpartition("::")
    return body or None


class _MemoryTransaction(Transaction):
    def __init__(self, client: MemoryClient):
        self._client = client

    async def commit(self) -> None:
        await self._client.commit_staged()

    async def rollback(self) -> None:
        self._client.clear_staged()


class _MemoryExecution(Execution):
    def __init__(self, store: MemorySessionStore, execution_id: ExecutionId):
        self._store = store
        self._id = execution_id

    async def complete(self, value: Any, return_type: type) -> None:
        self._store._client.stage_write(
            self._store._id_to_key(self._id, "status"), ExecutionStatus.COMPLETED
        )
        self._store._client.stage_write(
            self._store._id_to_key(self._id, "result"),
            self._store._serializer.serialize(value, return_type),
        )

    async def fail(self, error: str) -> None:
        self._store._client.stage_write(
            self._store._id_to_key(self._id, "status"), ExecutionStatus.FAILED
        )
        self._store._client.stage_write(
            self._store._id_to_key(self._id, "error"), error
        )


class MemorySessionStore(SessionStore):
    """
    An in-memory implementation of SessionStore for testing and development.
    This implementation is not persistent across processes.
    It serializes values to ensure independence, mimicking persisted stores.
    """

    def __init__(self, client: MemoryClient, serializer: Serializer, **_):
        self._client = client
        self._serializer = serializer
        self._lock = asyncio.Lock()

    def _id_to_key(self, id: ExecutionId, part: str) -> str:
        return _make_key(id.to_key(), part)

    async def begin_transaction(self) -> Transaction:
        return _MemoryTransaction(self._client)

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        status_key = self._id_to_key(execution_id, "status")
        if await self._client.read(status_key) is None:
            self._client.stage_write(status_key, ExecutionStatus.STARTED)
        return _MemoryExecution(self, execution_id)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        status: ExecutionStatus | None = await self._client.read(
            self._id_to_key(execution_id, "status")
        )
        if not status:
            return None

        result = None
        error = None

        if status == ExecutionStatus.COMPLETED:
            serialized_value = await self._client.read(
                self._id_to_key(execution_id, "result")
            )
            if serialized_value:
                result = self._serializer.deserialize(serialized_value, return_type)
        elif status == ExecutionStatus.FAILED:
            error = await self._client.read(self._id_to_key(execution_id, "error"))

        return ExecutionRecord(status=status, result=result, error=error)

    async def get_descendants(
        self, execution_id: ExecutionId
    ) -> list[ExecutionId]:
        prefix = execution_id.descendant_key_prefix()
        paths: set[str] = set()
        for key in self._client.all_keys():
            path = _key_to_path(key)
            if path is not None and path.startswith(prefix):
                paths.add(path)
        return [ExecutionId.from_key(p) for p in paths]

    async def delete_executions(
        self, execution_ids: Iterable[ExecutionId]
    ) -> None:
        for execution_id in execution_ids:
            for part in _PARTS:
                self._client.stage_delete(self._id_to_key(execution_id, part))
