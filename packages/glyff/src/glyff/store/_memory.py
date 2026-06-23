from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from .._interfaces import Execution, Serializer, SessionStore, Transaction
from .._models import ExecutionId, ExecutionRecord, ExecutionStatus
from ._memory_client import MemoryClient
from .utils import execution_id_to_path, path_to_execution_id

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
        self._closed = False
        # Isolate this transaction's staging from any concurrent transaction.
        self._token = client.begin_staging()

    async def commit(self) -> None:
        if self._closed:
            return
        try:
            await self._client.commit_staged()
        finally:
            self._client.end_staging(self._token)
            self._closed = True

    async def rollback(self) -> None:
        if self._closed:
            return
        try:
            self._client.clear_staged()
        finally:
            self._client.end_staging(self._token)
            self._closed = True


class _MemoryExecution(Execution):
    def __init__(
        self,
        client: MemoryClient,
        serializer: Serializer,
        execution_id: ExecutionId,
    ):
        self._client = client
        self._serializer = serializer
        self._id = execution_id

    async def complete(self, value: Any, return_type: type) -> None:
        path = execution_id_to_path(self._id)
        self._client.stage_write(_make_key(path, "status"), ExecutionStatus.COMPLETED)
        serialized_result = await self._serializer.serialize(value, return_type)
        self._client.stage_write(
            _make_key(path, "result"),
            serialized_result,
        )

    async def fail(self, error: str) -> None:
        path = execution_id_to_path(self._id)
        self._client.stage_write(_make_key(path, "status"), ExecutionStatus.FAILED)
        self._client.stage_write(_make_key(path, "error"), error)


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
        return _make_key(execution_id_to_path(id), part)

    async def begin_transaction(self) -> Transaction:
        return _MemoryTransaction(self._client)

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        status_key = self._id_to_key(execution_id, "status")
        if await self._client.read(status_key) is None:
            self._client.stage_write(status_key, ExecutionStatus.STARTED)
        return _MemoryExecution(self._client, self._serializer, execution_id)

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
                result = await self._serializer.deserialize(
                    serialized_value, return_type
                )
        elif status == ExecutionStatus.FAILED:
            error = await self._client.read(self._id_to_key(execution_id, "error"))

        return ExecutionRecord(status=status, result=result, error=error)

    async def get_descendants(self, execution_id: ExecutionId) -> list[ExecutionId]:
        prefix = execution_id_to_path(execution_id) + "/"
        paths: set[str] = set()
        for key in self._client.all_keys():
            path = _key_to_path(key)
            if path is not None and path.startswith(prefix):
                paths.add(path)
        return [path_to_execution_id(p) for p in paths]

    async def delete_executions(self, execution_ids: Iterable[ExecutionId]) -> None:
        for execution_id in execution_ids:
            for part in _PARTS:
                self._client.stage_delete(self._id_to_key(execution_id, part))
