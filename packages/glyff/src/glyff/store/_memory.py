from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import AsyncIterator, Iterable

from .._interfaces import ExecutionRepository, Transaction, TransactionProvider
from .._models import (
    CanonicalArguments,
    Execution,
    ExecutionId,
    ExecutionStatus,
    Metadata,
    SerializedValue,
    SessionId,
)
from ..exceptions import InvalidExecutionError
from ._memory_client import MemoryClient, MemoryKey
from .utils import execution_id_to_path, path_to_execution_id

_PARTS = ("arguments", "status", "result", "metadata")


def _make_key(session_id: SessionId, path: str, part: str) -> MemoryKey:
    return (session_id.value, path, part)


def _key_to_path(key: MemoryKey, session_id: SessionId) -> str | None:
    session, path, part = key
    if session != session_id.value or part != "status":
        return None
    return path or None


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

    def _id_to_key(
        self, session_id: SessionId, execution_id: ExecutionId, part: str
    ) -> MemoryKey:
        return _make_key(session_id, execution_id_to_path(execution_id), part)

    async def get(
        self, session_id: SessionId, execution_id: ExecutionId
    ) -> Execution | None:
        key = functools.partial(self._id_to_key, session_id, execution_id)
        status = await self._client.read(key("status"))
        if status is None:
            return None

        arguments_data = await self._client.read(key("arguments"))
        if not isinstance(arguments_data, bytes):
            raise InvalidExecutionError(
                f"Execution {execution_id} is stored without its arguments."
            )
        result_data = await self._client.read(key("result"))
        raw_metadata = await self._client.read(key("metadata"))

        metadata: dict[str, Metadata] = {}
        if isinstance(raw_metadata, dict):
            for name, value in raw_metadata.items():
                if isinstance(value, bytes):
                    metadata[name] = Metadata(key=name, value=SerializedValue(value))

        return Execution(
            id=execution_id,
            status=status,
            arguments=CanonicalArguments(arguments_data),
            result=SerializedValue(result_data)
            if isinstance(result_data, bytes)
            else None,
            metadata=metadata,
        )

    async def save(self, session_id: SessionId, execution: Execution) -> None:
        key = functools.partial(self._id_to_key, session_id, execution.id)
        self._client.stage_write(key("status"), execution.status)
        self._client.stage_write(key("arguments"), execution.arguments.data)

        if execution.result is not None:
            self._client.stage_write(key("result"), execution.result.data)
        else:
            self._client.stage_delete(key("result"))

        if execution.metadata:
            self._client.stage_write(
                key("metadata"),
                {name: item.value.data for name, item in execution.metadata.items()},
            )
        else:
            self._client.stage_delete(key("metadata"))

    async def executions(
        self,
        session_id: SessionId,
        *,
        status: ExecutionStatus | None = None,
        under: ExecutionId | None = None,
    ) -> AsyncIterator[Execution]:
        prefix = execution_id_to_path(under) + "/" if under is not None else ""
        paths = sorted(
            path
            for path in (
                _key_to_path(key, session_id) for key in self._client.all_keys()
            )
            if path is not None and path.startswith(prefix)
        )
        for path in paths:
            execution = await self.get(session_id, path_to_execution_id(path))
            if execution is not None and status in (None, execution.status):
                yield execution

    async def delete_many(
        self, session_id: SessionId, execution_ids: Iterable[ExecutionId]
    ) -> None:
        for execution_id in execution_ids:
            for part in _PARTS:
                self._client.stage_delete(
                    self._id_to_key(session_id, execution_id, part)
                )


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
        self._app_versions: dict[str, str] = {}
        self._claim_lock = asyncio.Lock()

    async def claim_session(self, session_id: SessionId, app_version: str) -> str:
        # Nothing here outlives the process, so the claim only has to hold for
        # as long as the records do.
        async with self._claim_lock:
            return self._app_versions.setdefault(session_id.value, app_version)
