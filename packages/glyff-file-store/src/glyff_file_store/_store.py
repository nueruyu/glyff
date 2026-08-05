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

from glyff.store.execution_stage import ExecutionStage, SaveExecution

from ._file_client import FileClient
from ._transaction import _ClientTransaction

# Bump when the stored layout changes.
FORMAT_VERSION = 1


class FileExecutionRepository(ExecutionRepository):
    """File-backed Execution aggregate repository."""

    def __init__(self, client: FileClient, stage: ExecutionStage):
        self._client = client
        self._stage = stage

    async def get(
        self, session_id: SessionId, execution_id: ExecutionId
    ) -> Execution | None:
        mutation = self._stage.lookup(session_id, execution_id)
        if mutation is not None:
            return (
                mutation.snapshot.to_execution()
                if isinstance(mutation, SaveExecution)
                else None
            )

        executions = await self._client.read_committed_executions(session_id.value)
        stored = executions.get(execution_id_to_path(execution_id))
        return None if stored is None else execution_from_dict(execution_id, stored)

    async def save(self, session_id: SessionId, execution: Execution) -> None:
        self._stage.save(session_id, execution)

    async def executions(
        self,
        session_id: SessionId,
        *,
        status: ExecutionStatus | None = None,
        under: ExecutionId | None = None,
    ) -> AsyncIterator[Execution]:
        prefix = execution_id_to_path(under) + "/" if under is not None else ""
        visible = await self._client.read_committed_executions(session_id.value)

        for key, mutation in self._stage.current_snapshot().items():
            if key.session_id != session_id:
                continue
            path = execution_id_to_path(key.execution_id)
            if isinstance(mutation, SaveExecution):
                visible[path] = execution_to_dict(mutation.snapshot.to_execution())
            else:
                visible.pop(path, None)

        for path, stored in sorted(visible.items()):
            if not path.startswith(prefix):
                continue
            execution = execution_from_dict(path_to_execution_id(path), stored)
            if status in (None, execution.status):
                yield execution

    async def delete_many(
        self, session_id: SessionId, execution_ids: Iterable[ExecutionId]
    ) -> None:
        for execution_id in execution_ids:
            self._stage.delete(session_id, execution_id)


class FileTransactionProvider(TransactionProvider):
    def __init__(self, client: FileClient, stage: ExecutionStage):
        self._client = client
        self._stage = stage

    async def begin_transaction(self) -> Transaction:
        return await _ClientTransaction(self._client, self._stage).begin()


class JsonFileBackend:
    """A file-backed backend for glyff, intended for debugging and inspection.

    Every session in the store lives in one pretty-printed JSON document under
    ``base_dir`` (see the README for its layout), read whole on access and
    rewritten on every commit — which is what makes it unsuitable for
    high-throughput or large-scale use.

    It requires a serializer that produces UTF-8 JSON text bytes, such as
    JsonSerializer or PydanticSerializer, because execution results and metadata
    are stored as embedded JSON values.
    """

    def __init__(self, *, base_dir: str | Path):
        client = FileClient(base_dir, format_version=FORMAT_VERSION)
        stage = ExecutionStage()
        self._client = client
        self.repository: ExecutionRepository = FileExecutionRepository(client, stage)
        self.transaction_provider: TransactionProvider = FileTransactionProvider(
            client, stage
        )

    async def claim_session(self, session_id: SessionId, app_version: str) -> str:
        return await self._client.claim_session(session_id.value, app_version)
