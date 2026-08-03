from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Iterable

from .._interfaces import ExecutionRepository, Transaction, TransactionProvider
from .._models import (
    EncodedArguments,
    Execution,
    ExecutionId,
    Metadata,
    SerializedValue,
)
from ..exceptions import InvalidExecutionError
from ._memory_client import MemoryClient
from .utils import execution_id_to_path, path_to_execution_id

_KEY_PREFIX = "execution::"
_PARTS = ("args", "status", "result", "metadata")


def _make_key(path: str, part: str) -> str:
    return f"{_KEY_PREFIX}{path}::{part}"


def _key_to_path(key: str) -> str | None:
    if not key.startswith(_KEY_PREFIX):
        return None
    body, _, part = key[len(_KEY_PREFIX) :].rpartition("::")
    if part != "status":
        return None
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


class MemoryExecutionRepository(ExecutionRepository):
    """In-memory Execution aggregate repository."""

    def __init__(self, client: MemoryClient):
        self._client = client

    def _id_to_key(self, execution_id: ExecutionId, part: str) -> str:
        return _make_key(execution_id_to_path(execution_id), part)

    async def get(self, execution_id: ExecutionId) -> Execution | None:
        status = await self._client.read(self._id_to_key(execution_id, "status"))
        if status is None:
            return None

        args_data = await self._client.read(self._id_to_key(execution_id, "args"))
        if not isinstance(args_data, bytes):
            raise InvalidExecutionError(
                f"Execution {execution_id} is stored without its arguments."
            )
        result_data = await self._client.read(self._id_to_key(execution_id, "result"))
        raw_metadata = await self._client.read(
            self._id_to_key(execution_id, "metadata")
        )

        metadata: dict[str, Metadata] = {}
        if isinstance(raw_metadata, dict):
            for key, value in raw_metadata.items():
                if isinstance(value, bytes):
                    metadata[key] = Metadata(key=key, value=SerializedValue(value))

        return Execution(
            id=execution_id,
            status=status,
            args=EncodedArguments(args_data),
            result=SerializedValue(result_data)
            if isinstance(result_data, bytes)
            else None,
            metadata=metadata,
        )

    async def save(self, execution: Execution) -> None:
        self._client.stage_write(
            self._id_to_key(execution.id, "status"),
            execution.status,
        )
        self._client.stage_write(
            self._id_to_key(execution.id, "args"),
            execution.args.data,
        )

        if execution.result is not None:
            self._client.stage_write(
                self._id_to_key(execution.id, "result"),
                execution.result.data,
            )
        else:
            self._client.stage_delete(self._id_to_key(execution.id, "result"))

        if execution.metadata:
            self._client.stage_write(
                self._id_to_key(execution.id, "metadata"),
                {key: item.value.data for key, item in execution.metadata.items()},
            )
        else:
            self._client.stage_delete(self._id_to_key(execution.id, "metadata"))

    async def descendants_of(self, execution_id: ExecutionId) -> list[ExecutionId]:
        prefix = execution_id_to_path(execution_id) + "/"
        paths: set[str] = set()
        for key in self._client.all_keys():
            path = _key_to_path(key)
            if path is not None and path.startswith(prefix):
                paths.add(path)
        return [path_to_execution_id(p) for p in paths]

    async def delete_many(self, execution_ids: Iterable[ExecutionId]) -> None:
        for execution_id in execution_ids:
            for part in _PARTS:
                self._client.stage_delete(self._id_to_key(execution_id, part))


class MemoryTransactionProvider(TransactionProvider):
    def __init__(self, client: MemoryClient):
        self._client = client

    async def begin_transaction(self) -> Transaction:
        return await _MemoryTransaction(self._client).begin()


class MemoryBackend:
    def __init__(self) -> None:
        client = MemoryClient()
        self.repository: ExecutionRepository = MemoryExecutionRepository(client)
        self.transaction_provider: TransactionProvider = MemoryTransactionProvider(
            client
        )
