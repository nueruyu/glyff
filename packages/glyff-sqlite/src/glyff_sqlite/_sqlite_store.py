from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from glyff import (
    Execution,
    ExecutionId,
    ExecutionStatus,
    Metadata,
    SerializedValue,
    Serializer,
    SessionStore,
    Transaction,
)
from glyff.serialization.constants import DEFAULT_ENCODING
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._sqlite_client import SQLiteClient
from ._transaction import _ClientTransaction

logger = logging.getLogger(__name__)

_EXECUTIONS_NAMESPACE = "executions"

_STATUS_NAMES = {
    ExecutionStatus.STARTED: "started",
    ExecutionStatus.COMPLETED: "completed",
    ExecutionStatus.FAILED: "failed",
}
_NAME_TO_STATUS = {v: k for k, v in _STATUS_NAMES.items()}


def _to_stored_bytes(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True).encode(
        DEFAULT_ENCODING
    )


def _to_execution(execution_id: ExecutionId, data: bytes) -> Execution:
    stored = json.loads(data.decode(DEFAULT_ENCODING))
    metadata_raw = stored.get("metadata")
    metadata: dict[str, Metadata] = {}
    if isinstance(metadata_raw, dict):
        for key, value_json in metadata_raw.items():
            if isinstance(value_json, str):
                metadata[key] = Metadata(
                    key=key,
                    value=SerializedValue(value_json.encode(DEFAULT_ENCODING)),
                )

    result_json = stored.get("result_json")
    return Execution(
        id=execution_id,
        status=_NAME_TO_STATUS[stored["status"]],
        result=(
            SerializedValue(result_json.encode(DEFAULT_ENCODING))
            if isinstance(result_json, str)
            else None
        ),
        error=stored.get("error") if isinstance(stored.get("error"), str) else None,
        metadata=metadata,
    )


def _from_execution(execution: Execution) -> bytes:
    return _to_stored_bytes(
        {
            "status": _STATUS_NAMES[execution.status],
            "result_json": (
                execution.result.data.decode(DEFAULT_ENCODING)
                if execution.result is not None
                else None
            ),
            "error": execution.error,
            "metadata": {
                key: item.value.data.decode(DEFAULT_ENCODING)
                for key, item in execution.metadata.items()
            },
        }
    )


class SQLiteExecutionRepository(SessionStore):
    """SQLite-backed Execution aggregate repository."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        serializer: Serializer | None = None,
        *,
        client: SQLiteClient | None = None,
        busy_timeout_ms: int = 30_000,
        synchronous: str = "FULL",
    ):
        if client is None:
            if database_path is None:
                raise TypeError("database_path or client is required.")
            client = SQLiteClient(
                database_path,
                busy_timeout_ms=busy_timeout_ms,
                synchronous=synchronous,
            )
        elif database_path is not None:
            raise TypeError("Pass either database_path or client, not both.")

        self._client = client
        self.serializer = serializer
        self._client._initialize_schema_sync()

    async def begin_transaction(self) -> Transaction:
        return await _ClientTransaction(self._client).begin()

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


SQLiteSessionStore = SQLiteExecutionRepository
