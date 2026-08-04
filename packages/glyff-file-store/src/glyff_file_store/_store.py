from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from pathlib import Path

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRepository,
    ExecutionStatus,
    SessionId,
    Transaction,
    TransactionProvider,
)
from glyff.store.aggregate_codec import execution_from_dict, execution_to_dict
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._file_client import Executions, FileClient
from ._transaction import _ClientTransaction

# Bump when the stored layout changes.
FORMAT_VERSION = 1


class FileExecutionRepository(ExecutionRepository):
    """File-backed Execution aggregate repository."""

    def __init__(self, client: FileClient):
        self._client = client

    async def get(
        self, session_id: SessionId, execution_id: ExecutionId
    ) -> Execution | None:
        executions = await self._client.read_executions(session_id.value)
        stored = executions.get(execution_id_to_path(execution_id))
        if stored is None:
            return None
        return execution_from_dict(execution_id, stored)

    async def save(self, session_id: SessionId, execution: Execution) -> None:
        path = execution_id_to_path(execution.id)
        stored = execution_to_dict(execution)

        def update(executions: Executions) -> Executions:
            return {**executions, path: stored}

        self._client.stage_executions(session_id.value, update)

    async def executions(
        self,
        session_id: SessionId,
        *,
        status: ExecutionStatus | None = None,
        under: ExecutionId | None = None,
    ) -> AsyncIterator[Execution]:
        prefix = execution_id_to_path(under) + "/" if under is not None else ""
        recorded = await self._client.read_executions(session_id.value)
        for path, stored in sorted(recorded.items()):
            if not path.startswith(prefix):
                continue
            execution = execution_from_dict(path_to_execution_id(path), stored)
            if status in (None, execution.status):
                yield execution

    async def delete_many(
        self, session_id: SessionId, execution_ids: Iterable[ExecutionId]
    ) -> None:
        paths = {execution_id_to_path(eid) for eid in execution_ids}
        if not paths:
            return

        def update(executions: Executions) -> Executions:
            return {
                path: stored for path, stored in executions.items() if path not in paths
            }

        self._client.stage_executions(session_id.value, update)


class FileTransactionProvider(TransactionProvider):
    def __init__(self, client: FileClient):
        self._client = client

    async def begin_transaction(self) -> Transaction:
        return await _ClientTransaction(self._client).begin()


class JsonFileBackend:
    """A file-backed backend for glyff, intended for debugging and inspection.

    Every session in the store lives in one pretty-printed, key-sorted JSON
    document under ``base_dir``, nested by session id::

        {"format_version": 1,
         "sessions": {"orders": {"app_version": "v1", "executions": {...}}}}

    It requires a serializer that produces UTF-8 JSON text bytes, such as
    JsonSerializer or PydanticSerializer, because execution results and metadata
    are stored as embedded JSON values.

    The whole document is read on access and rewritten on every commit, which is
    what keeps it readable and what makes it unsuitable for high-throughput or
    large-scale use.
    """

    def __init__(self, *, base_dir: str | Path):
        client = FileClient(base_dir, format_version=FORMAT_VERSION)
        self._client = client
        self.repository: ExecutionRepository = FileExecutionRepository(client)
        self.transaction_provider: TransactionProvider = FileTransactionProvider(client)

    async def claim_session(self, session_id: SessionId, app_version: str) -> str:
        return await self._client.claim_session(session_id.value, app_version)
