from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from pathlib import Path

from glyff import (
    DomainId,
    DomainVersion,
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

from glyff.migration import MigratableBackend, SessionMigration
from glyff.store.staging import ExecutionStaging, SaveExecution

from ._file_client import FileClient
from ._file_migration import FileSessionMigration
from ._transaction import ClientTransaction

# Bump when the stored layout changes.
FORMAT_VERSION = 1


class FileExecutionRepository(ExecutionRepository):
    """File-backed Execution aggregate repository."""

    def __init__(self, client: FileClient, staging: ExecutionStaging):
        self._client = client
        self._staging = staging

    async def get(
        self, session_id: SessionId, execution_id: ExecutionId
    ) -> Execution | None:
        stage = self._staging.current()
        mutation = stage.lookup(session_id, execution_id) if stage else None
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
        self._staging.require_current().save(session_id, execution)

    async def executions(
        self,
        session_id: SessionId,
        *,
        status: ExecutionStatus | None = None,
        under: ExecutionId | None = None,
    ) -> AsyncIterator[Execution]:
        prefix = execution_id_to_path(under) + "/" if under is not None else ""
        visible = await self._client.read_committed_executions(session_id.value)

        stage = self._staging.current()
        for key, mutation in (stage.snapshot() if stage else {}).items():
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
        stage = self._staging.require_current()
        for execution_id in execution_ids:
            stage.delete(session_id, execution_id)


class FileTransactionProvider(TransactionProvider):
    def __init__(self, client: FileClient, staging: ExecutionStaging):
        self._client = client
        self._staging = staging

    async def begin_transaction(self) -> Transaction:
        return await ClientTransaction(self._client, self._staging).begin()


class JsonFileBackend(MigratableBackend):
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
        staging = ExecutionStaging()
        self._client = client
        self._repository = FileExecutionRepository(client, staging)
        self._transaction_provider = FileTransactionProvider(client, staging)
        self._session_migration = FileSessionMigration(client)

    @property
    def repository(self) -> ExecutionRepository:
        return self._repository

    @property
    def transaction_provider(self) -> TransactionProvider:
        return self._transaction_provider

    @property
    def session_migration(self) -> SessionMigration:
        return self._session_migration

    async def claim_domain(
        self,
        session_id: SessionId,
        domain_id: DomainId,
        version: DomainVersion,
    ) -> DomainVersion:
        return DomainVersion(
            await self._client.claim_domain(session_id.value, domain_id, version.value)
        )
