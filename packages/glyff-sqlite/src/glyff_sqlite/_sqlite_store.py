from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRepository,
    Transaction,
    TransactionProvider,
)
from glyff.serialization.constants import DEFAULT_ENCODING
from glyff.store.aggregate_codec import execution_from_dict, execution_to_dict
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._sqlite_client import SQLiteClient
from ._transaction import _ClientTransaction

_EXECUTIONS_NAMESPACE = "executions"


def _to_execution(execution_id: ExecutionId, data: bytes) -> Execution:
    return execution_from_dict(execution_id, json.loads(data.decode(DEFAULT_ENCODING)))


def _from_execution(execution: Execution) -> bytes:
    return json.dumps(
        execution_to_dict(execution), ensure_ascii=False, sort_keys=True
    ).encode(DEFAULT_ENCODING)


class SQLiteExecutionRepository(ExecutionRepository):
    """SQLite-backed Execution aggregate repository."""

    def __init__(self, client: SQLiteClient):
        self._client = client

    async def get(self, execution_id: ExecutionId) -> Execution | None:
        key = execution_id_to_path(execution_id)
        data = await self._client.read(_EXECUTIONS_NAMESPACE, key, staged=True)
        if data is None:
            return None
        return _to_execution(execution_id, data)

    async def save(self, execution: Execution) -> None:
        key = execution_id_to_path(execution.id)
        self._client.stage_write(
            _EXECUTIONS_NAMESPACE,
            key,
            _from_execution(execution),
        )

    async def descendants_of(self, execution_id: ExecutionId) -> list[ExecutionId]:
        prefix = execution_id_to_path(execution_id) + "/"
        keys = await self._client.list_keys(_EXECUTIONS_NAMESPACE, prefix, staged=True)
        return [path_to_execution_id(k) for k in keys]

    async def delete_many(self, execution_ids: Iterable[ExecutionId]) -> None:
        for execution_id in execution_ids:
            self._client.stage_delete(
                _EXECUTIONS_NAMESPACE,
                execution_id_to_path(execution_id),
            )


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
