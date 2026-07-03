from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Iterable
from typing import Any

from .._interfaces import Execution, Serializer, SessionStore, Transaction
from .._models import ExecutionId, ExecutionRecord, ExecutionStatus
from ._memory_client import MemoryClient
from .utils import execution_id_to_path, path_to_execution_id

_KEY_PREFIX = "execution::"
_PARTS = ("status", "result", "error", "metadata")


def _make_key(path: str, part: str) -> str:
    return f"{_KEY_PREFIX}{path}::{part}"


def _key_to_path(key: str) -> str | None:
    if not key.startswith(_KEY_PREFIX):
        return None
    body, _, _ = key[len(_KEY_PREFIX) :].rpartition("::")
    return body or None


class _MemoryTransaction(Transaction):
    def __init__(self, client: MemoryClient):
        self._client = client
        self._closed = False
        self._lock = asyncio.Lock()
        self._token: contextvars.Token | None = None
        self._staging = None

    async def begin(self) -> _MemoryTransaction:
        self._token, self._staging = self._client.begin_staging()
        return self

    async def commit(self) -> None:
        async with self._lock:
            if self._closed:
                return
            if self._staging is None:
                raise RuntimeError("transaction not started")
            self._client._require_current_staging(self._staging)
            self._closed = True
            try:
                await self._client.commit_staged()
            finally:
                if self._token is None:
                    raise RuntimeError("transaction not started")
                self._client.end_staging(self._token)

    async def rollback(self) -> None:
        async with self._lock:
            if self._closed:
                return
            if self._staging is None:
                raise RuntimeError("transaction not started")
            self._client._require_current_staging(self._staging)
            self._closed = True
            try:
                self._client.clear_staged()
            finally:
                if self._token is None:
                    raise RuntimeError("transaction not started")
                self._client.end_staging(self._token)


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


class MemoryExecutionRepository:
    """Persistence for execution records over a :class:`MemoryClient`.

    Owns the ExecutionId<->key mapping, record codec, metadata, descendant
    queries, and deletion. The store owns transactions; writes stage into the
    client.
    """

    def __init__(self, client: MemoryClient, serializer: Serializer):
        self._client = client
        self._serializer = serializer

    def _id_to_key(self, id: ExecutionId, part: str) -> str:
        return _make_key(execution_id_to_path(id), part)

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

    async def set_metadata(
        self, execution_id: ExecutionId, key: str, value: Any, value_type: type
    ) -> None:
        if await self._client.read(self._id_to_key(execution_id, "status")) is None:
            raise LookupError(f"Execution {execution_id} not found")
        meta_key = self._id_to_key(execution_id, "metadata")
        current = await self._client.read(meta_key)
        metadata = dict(current) if isinstance(current, dict) else {}
        metadata[key] = await self._serializer.serialize(value, value_type)
        self._client.stage_write(meta_key, metadata)

    async def get_metadata(
        self, execution_id: ExecutionId, key: str, return_type: type
    ) -> Any | None:
        if await self._client.read(self._id_to_key(execution_id, "status")) is None:
            return None
        metadata = await self._client.read(self._id_to_key(execution_id, "metadata"))
        if not isinstance(metadata, dict) or key not in metadata:
            return None
        return await self._serializer.deserialize(metadata[key], return_type)


class MemorySessionStore(SessionStore):
    """An in-memory SessionStore for testing and development.

    Owns the transaction boundary; delegates persistence to
    :class:`MemoryExecutionRepository`, exposed as ``repository``.
    """

    def __init__(self, serializer: Serializer, client: MemoryClient | None = None, **_):
        client = client if client is not None else MemoryClient()
        self._client = client
        self._repository = MemoryExecutionRepository(client, serializer)

    @property
    def repository(self) -> MemoryExecutionRepository:
        return self._repository

    async def begin_transaction(self) -> Transaction:
        return await _MemoryTransaction(self._client).begin()

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        return await self._repository.start_execution(execution_id)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        return await self._repository.get_execution_record(execution_id, return_type)

    async def set_metadata(
        self, execution_id: ExecutionId, key: str, value: Any, value_type: type
    ) -> None:
        await self._repository.set_metadata(execution_id, key, value, value_type)

    async def get_metadata(
        self, execution_id: ExecutionId, key: str, return_type: type
    ) -> Any | None:
        return await self._repository.get_metadata(execution_id, key, return_type)
