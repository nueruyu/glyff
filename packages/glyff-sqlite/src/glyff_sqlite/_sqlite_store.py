from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRepository,
    ExecutionStatus,
    Metadata,
    SerializedValue,
    Transaction,
    TransactionProvider,
)
from glyff.serialization.constants import DEFAULT_ENCODING
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._sqlite_client import SQLiteClient
from ._transaction import _ClientTransaction

_EXECUTIONS_NAMESPACE = "executions"

_STATUS_NAMES = {
    ExecutionStatus.STARTED: "started",
    ExecutionStatus.COMPLETED: "completed",
    ExecutionStatus.FAILED: "failed",
}
_NAME_TO_STATUS = {v: k for k, v in _STATUS_NAMES.items()}


def _pack_value(value: SerializedValue | None) -> str | None:
    if value is None:
        return None
    return base64.b64encode(value.data).decode("ascii")


def _unpack_value(value: object) -> SerializedValue | None:
    if not isinstance(value, str):
        return None
    return SerializedValue(base64.b64decode(value.encode("ascii")))


def _pack_metadata(metadata: dict[str, Metadata]) -> dict[str, str]:
    return {
        key: base64.b64encode(item.value.data).decode("ascii")
        for key, item in metadata.items()
    }


def _unpack_metadata(raw: object) -> dict[str, Metadata]:
    if not isinstance(raw, dict):
        return {}

    result: dict[str, Metadata] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str):
            result[key] = Metadata(
                key=key,
                value=SerializedValue(base64.b64decode(value.encode("ascii"))),
            )
    return result


def _to_stored_bytes(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True).encode(
        DEFAULT_ENCODING
    )


def _to_execution(execution_id: ExecutionId, data: bytes) -> Execution:
    stored = json.loads(data.decode(DEFAULT_ENCODING))
    return Execution(
        id=execution_id,
        status=_NAME_TO_STATUS[stored["status"]],
        result=_unpack_value(stored.get("result_b64")),
        error=stored.get("error") if isinstance(stored.get("error"), str) else None,
        metadata=_unpack_metadata(stored.get("metadata")),
    )


def _from_execution(execution: Execution) -> bytes:
    return _to_stored_bytes(
        {
            "status": _STATUS_NAMES[execution.status],
            "result_b64": _pack_value(execution.result),
            "error": execution.error,
            "metadata": _pack_metadata(execution.metadata),
        }
    )


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
