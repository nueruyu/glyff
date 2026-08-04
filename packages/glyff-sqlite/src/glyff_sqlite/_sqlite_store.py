from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRepository,
    ExecutionStatus,
    SessionId,
    Transaction,
    TransactionProvider,
)
from glyff.serialization.constants import JSON_SEPARATORS
from glyff.store.aggregate_codec import execution_from_dict, execution_to_dict
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._sqlite_client import SQLiteClient, SQLiteExecutionRecord
from ._transaction import _ClientTransaction


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=JSON_SEPARATORS,
    )


def _to_execution(
    execution_id: ExecutionId, record: SQLiteExecutionRecord
) -> Execution:
    stored = {
        "arguments": record.arguments,
        "status": record.status,
        "result": json.loads(record.result) if record.result is not None else None,
        "metadata": json.loads(record.metadata),
    }
    return execution_from_dict(execution_id, stored)


def _from_execution(execution: Execution) -> SQLiteExecutionRecord:
    stored = execution_to_dict(execution)
    return SQLiteExecutionRecord(
        arguments=stored["arguments"],
        status=stored["status"],
        result=_json_text(stored["result"]) if execution.result is not None else None,
        metadata=_json_text(stored["metadata"]),
    )


class SQLiteExecutionRepository(ExecutionRepository):
    """SQLite-backed Execution aggregate repository."""

    def __init__(self, client: SQLiteClient):
        self._client = client

    async def get(
        self, session_id: SessionId, execution_id: ExecutionId
    ) -> Execution | None:
        key = (session_id.value, execution_id_to_path(execution_id))
        record = await self._client.read(key, staged=True)
        if record is None:
            return None
        return _to_execution(execution_id, record)

    async def save(self, session_id: SessionId, execution: Execution) -> None:
        key = (session_id.value, execution_id_to_path(execution.id))
        self._client.stage_write(key, _from_execution(execution))

    async def executions(
        self,
        session_id: SessionId,
        *,
        status: ExecutionStatus | None = None,
        under: ExecutionId | None = None,
    ) -> AsyncIterator[Execution]:
        prefix = execution_id_to_path(under) + "/" if under is not None else ""
        # Status is filtered here rather than in SQL: a staged record can differ
        # in status from the committed row a WHERE clause would have judged it by.
        async for path, record in self._client.iter_records(
            session_id.value, prefix, staged=True
        ):
            execution = _to_execution(path_to_execution_id(path), record)
            if status in (None, execution.status):
                yield execution

    async def delete_many(
        self, session_id: SessionId, execution_ids: Iterable[ExecutionId]
    ) -> None:
        for execution_id in execution_ids:
            self._client.stage_delete(
                (session_id.value, execution_id_to_path(execution_id))
            )


class SQLiteTransactionProvider(TransactionProvider):
    def __init__(self, client: SQLiteClient):
        self._client = client

    async def begin_transaction(self) -> Transaction:
        return await _ClientTransaction(self._client).begin()


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
        self._client = client
        self.repository: ExecutionRepository = SQLiteExecutionRepository(client)
        self.transaction_provider: TransactionProvider = SQLiteTransactionProvider(
            client
        )

    async def claim_session(
        self, session_id: SessionId, app_version: str | None
    ) -> str | None:
        return await self._client.claim_session(session_id.value, app_version)
