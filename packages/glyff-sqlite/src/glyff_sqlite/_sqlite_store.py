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
from glyff.store._execution_stage import (
    ExecutionMutation,
    ExecutionStage,
    SaveExecution,
)
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._sqlite_client import SQLiteClient
from ._transaction import _ClientTransaction


class SQLiteExecutionRepository(ExecutionRepository):
    """SQLite-backed Execution aggregate repository."""

    def __init__(self, client: SQLiteClient, stage: ExecutionStage):
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

        record = await self._client.read_committed(
            session_id.value, execution_id_to_path(execution_id)
        )
        return None if record is None else record.to_execution(execution_id)

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
        staged = self._staged_for(session_id, prefix)
        staged_paths = sorted(staged)
        next_staged = 0

        # The merge needs one order for both sides: SQLite's BINARY collation
        # compares UTF-8 bytes and Python compares code points, which agree,
        # because UTF-8 preserves code point order.
        async for path, record in self._client.iter_committed(session_id.value, prefix):
            while next_staged < len(staged_paths) and staged_paths[next_staged] < path:
                pending = staged_paths[next_staged]
                next_staged += 1
                execution = _staged_execution(staged[pending])
                if execution is not None and status in (None, execution.status):
                    yield execution

            if next_staged < len(staged_paths) and staged_paths[next_staged] == path:
                next_staged += 1
                execution = _staged_execution(staged[path])
                if execution is None:
                    continue
            else:
                execution = record.to_execution(path_to_execution_id(path))

            # Status is filtered after the overlay: a staged save can differ in
            # status from the committed row a WHERE clause would have judged.
            if status in (None, execution.status):
                yield execution

        for path in staged_paths[next_staged:]:
            execution = _staged_execution(staged[path])
            if execution is not None and status in (None, execution.status):
                yield execution

    async def delete_many(
        self, session_id: SessionId, execution_ids: Iterable[ExecutionId]
    ) -> None:
        for execution_id in execution_ids:
            self._stage.delete(session_id, execution_id)

    def _staged_for(
        self, session_id: SessionId, prefix: str
    ) -> dict[str, ExecutionMutation]:
        staged = {}
        for key, mutation in self._stage.current_snapshot().items():
            if key.session_id != session_id:
                continue
            path = execution_id_to_path(key.execution_id)
            if path.startswith(prefix):
                staged[path] = mutation
        return staged


def _staged_execution(mutation: ExecutionMutation) -> Execution | None:
    return (
        mutation.snapshot.to_execution()
        if isinstance(mutation, SaveExecution)
        else None
    )


class SQLiteTransactionProvider(TransactionProvider):
    def __init__(self, client: SQLiteClient, stage: ExecutionStage):
        self._client = client
        self._stage = stage

    async def begin_transaction(self) -> Transaction:
        return await _ClientTransaction(self._client, self._stage).begin()


class SQLiteBackend:
    """A durable, SQLite-backed backend for glyff.

    This backend stores each execution in a row in a SQLite database, providing
    transactional guarantees and indexed lookups. It is suitable for production
    use.

    It requires a serializer that produces UTF-8 JSON text bytes, such as
    JsonSerializer or PydanticSerializer, because execution results and metadata
    are stored as JSON text columns for readability and queryability.

    One database holds any number of sessions: records are keyed by
    ``(session_id, path)``, and each session's application version lives in a
    row of its own.

    ``table_prefix`` (default ``glyff``) names the three tables the store owns:
    ``<prefix>_executions`` for the records, ``<prefix>_sessions`` for their
    application versions, and ``<prefix>_meta`` for the store's format version.
    Set it to cohabit an application's database; a store written by an
    incompatible build is refused, and ``PRAGMA user_version`` is left to the
    application.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 30_000,
        synchronous: str = "FULL",
        table_prefix: str = "glyff",
    ):
        client = SQLiteClient(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            synchronous=synchronous,
            table_prefix=table_prefix,
        )
        client._initialize_schema_sync()
        stage = ExecutionStage()
        self._client = client
        self.repository: ExecutionRepository = SQLiteExecutionRepository(client, stage)
        self.transaction_provider: TransactionProvider = SQLiteTransactionProvider(
            client, stage
        )

    async def claim_session(self, session_id: SessionId, app_version: str) -> str:
        return await self._client.claim_session(session_id.value, app_version)
