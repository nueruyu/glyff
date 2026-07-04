from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRepository,
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


def _to_execution(execution_id: ExecutionId, record: SQLiteExecutionRecord) -> Execution:
    stored = {
        "status": record.status,
        "result": json.loads(record.result) if record.result is not None else None,
        "metadata": json.loads(record.metadata),
    }
    return execution_from_dict(execution_id, stored)


def _from_execution(execution: Execution) -> SQLiteExecutionRecord:
    stored = execution_to_dict(execution)
    return SQLiteExecutionRecord(
        status=stored["status"],
        result=_json_text(stored["result"]) if execution.result is not None else None,
        metadata=_json_text(stored["metadata"]),
    )


class SQLiteExecutionRepository(ExecutionRepository):
    """SQLite-backed Execution aggregate repository."""

    def __init__(self, client: SQLiteClient):
        self._client = client

    async def get(self, execution_id: ExecutionId) -> Execution | None:
        key = execution_id_to_path(execution_id)
        record = await self._client.read(key, staged=True)
        if record is None:
            return None
        return _to_execution(execution_id, record)

    async def save(self, execution: Execution) -> None:
        key = execution_id_to_path(execution.id)
        self._client.stage_write(key, _from_execution(execution))

    async def descendants_of(self, execution_id: ExecutionId) -> list[ExecutionId]:
        prefix = execution_id_to_path(execution_id) + "/"
        keys = await self._client.list_paths(prefix, staged=True)
        return [path_to_execution_id(k) for k in keys]

    async def delete_many(self, execution_ids: Iterable[ExecutionId]) -> None:
        for execution_id in execution_ids:
            self._client.stage_delete(execution_id_to_path(execution_id))


class SQLiteTransactionProvider(TransactionProvider):
    def __init__(self, client: SQLiteClient):
        self._client = client

    async def begin_transaction(self) -> Transaction:
        return await _ClientTransaction(self._client).begin()


class SQLiteBackend:
    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 30_000,
        synchronous: str = "FULL",
    ):
        client = SQLiteClient(
            database_path,
            busy_timeout_ms=busy_timeout_ms,
            synchronous=synchronous,
        )
        client._initialize_schema_sync()
        self.executions: ExecutionRepository = SQLiteExecutionRepository(client)
        self.transactions: TransactionProvider = SQLiteTransactionProvider(client)
