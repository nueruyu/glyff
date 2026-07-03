from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRecord,
    ExecutionStatus,
    SessionStore,
    Transaction,
)
from glyff.serialization import JsonSerializer
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


def _from_stored_bytes(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    return json.loads(data.decode(DEFAULT_ENCODING))


def _make_stored(
    status: ExecutionStatus,
    result_json: str | None = None,
    error: str | None = None,
) -> bytes:
    return _to_stored_bytes(
        {
            "status": _STATUS_NAMES[status],
            "result_json": result_json,
            "error": error,
        }
    )


async def _to_record(
    data: bytes,
    return_type: type,
    serializer: JsonSerializer,
) -> ExecutionRecord:
    stored = json.loads(data.decode(DEFAULT_ENCODING))
    status = _NAME_TO_STATUS[stored["status"]]
    result: Any | None = None
    error: str | None = None
    if status == ExecutionStatus.COMPLETED:
        result_json = stored.get("result_json")
        if result_json is not None:
            result = await serializer.deserialize(
                result_json.encode(DEFAULT_ENCODING), return_type
            )
    elif status == ExecutionStatus.FAILED:
        error = stored.get("error") or ""
    return ExecutionRecord(status=status, result=result, error=error)


class _SQLiteExecution(Execution):
    def __init__(
        self,
        client: SQLiteClient,
        execution_id: ExecutionId,
        serializer: JsonSerializer,
    ) -> None:
        self._client = client
        self._key = execution_id_to_path(execution_id)
        self._serializer = serializer

    async def complete(self, value: object, return_type: type) -> None:
        serialized_bytes = await self._serializer.serialize(value, return_type)
        result_json = serialized_bytes.decode(DEFAULT_ENCODING)

        def fn(data: bytes | None) -> bytes | None:
            if data is None:
                raise LookupError(f"Execution at {self._key} not found")
            stored = json.loads(data.decode(DEFAULT_ENCODING))
            if stored["status"] in (
                _STATUS_NAMES[ExecutionStatus.COMPLETED],
                _STATUS_NAMES[ExecutionStatus.FAILED],
            ):
                raise ValueError(
                    f"Cannot complete execution at {self._key}: "
                    f"already {stored['status']}"
                )
            stored["status"] = _STATUS_NAMES[ExecutionStatus.COMPLETED]
            stored["result_json"] = result_json
            return _to_stored_bytes(stored)

        self._client.stage_update(_EXECUTIONS_NAMESPACE, self._key, fn)

    async def fail(self, error: str) -> None:
        def fn(data: bytes | None) -> bytes | None:
            if data is None:
                raise LookupError(f"Execution at {self._key} not found")
            stored = json.loads(data.decode(DEFAULT_ENCODING))
            if stored["status"] in (
                _STATUS_NAMES[ExecutionStatus.COMPLETED],
                _STATUS_NAMES[ExecutionStatus.FAILED],
            ):
                raise ValueError(
                    f"Cannot fail execution at {self._key}: already {stored['status']}"
                )
            stored["status"] = _STATUS_NAMES[ExecutionStatus.FAILED]
            stored["error"] = error
            return _to_stored_bytes(stored)

        self._client.stage_update(_EXECUTIONS_NAMESPACE, self._key, fn)


class SQLiteExecutionRepository:
    """Persistence for execution records over a :class:`SQLiteClient`.

    Owns the ExecutionId<->key mapping, record codec, metadata, descendant
    queries, and deletion. Records live in the ``records`` table under the
    ``"executions"`` namespace; the store owns transactions.
    """

    def __init__(self, client: SQLiteClient, serializer: JsonSerializer):
        self._client = client
        self._serializer = serializer

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        key = execution_id_to_path(execution_id)
        existing = await self.get_execution_record(execution_id, type(None))
        if existing is None:

            def fn(data: bytes | None) -> bytes | None:
                if data is not None:
                    stored = json.loads(data.decode(DEFAULT_ENCODING))
                    if stored["status"] in (
                        _STATUS_NAMES[ExecutionStatus.COMPLETED],
                        _STATUS_NAMES[ExecutionStatus.FAILED],
                    ):
                        raise ValueError(
                            f"Cannot start execution {execution_id}: "
                            f"already {stored['status']}"
                        )
                return _make_stored(ExecutionStatus.STARTED)

            self._client.stage_update(_EXECUTIONS_NAMESPACE, key, fn)

        return _SQLiteExecution(self._client, execution_id, self._serializer)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        key = execution_id_to_path(execution_id)
        data = await self._client.read(_EXECUTIONS_NAMESPACE, key, staged=True)
        if data is None:
            return None
        return await _to_record(data, return_type, self._serializer)

    async def get_descendants(self, execution_id: ExecutionId) -> list[ExecutionId]:
        prefix = execution_id_to_path(execution_id) + "/"
        keys = await self._client.list_keys(_EXECUTIONS_NAMESPACE, prefix, staged=True)
        return [path_to_execution_id(k) for k in keys]

    async def delete_executions(self, execution_ids: Iterable[ExecutionId]) -> None:
        for execution_id in execution_ids:
            key = execution_id_to_path(execution_id)
            self._client.stage_delete(_EXECUTIONS_NAMESPACE, key)

    async def set_metadata(
        self, execution_id: ExecutionId, key: str, value: Any, value_type: type
    ) -> None:
        path = execution_id_to_path(execution_id)
        serialized = await self._serializer.serialize(value, value_type)
        value_json = serialized.decode(DEFAULT_ENCODING)

        def fn(data: bytes | None) -> bytes | None:
            if data is None:
                raise LookupError(f"Execution at {path} not found")
            stored = json.loads(data.decode(DEFAULT_ENCODING))
            metadata = stored.setdefault("metadata", {})
            metadata[key] = value_json
            return _to_stored_bytes(stored)

        self._client.stage_update(_EXECUTIONS_NAMESPACE, path, fn)

    async def get_metadata(
        self, execution_id: ExecutionId, key: str, return_type: type
    ) -> Any | None:
        path = execution_id_to_path(execution_id)
        data = await self._client.read(_EXECUTIONS_NAMESPACE, path, staged=True)
        if data is None:
            return None
        stored = json.loads(data.decode(DEFAULT_ENCODING))
        metadata = stored.get("metadata") or {}
        if key not in metadata:
            return None
        return await self._serializer.deserialize(
            metadata[key].encode(DEFAULT_ENCODING), return_type
        )


class SQLiteSessionStore(SessionStore):
    """A SQLite-backed SessionStore for durable local persistence.

    Owns the transaction boundary; delegates persistence to
    :class:`SQLiteExecutionRepository`, exposed as ``repository``.
    """

    def __init__(
        self,
        database_path: str | Path | None = None,
        serializer: JsonSerializer | None = None,
        *,
        client: SQLiteClient | None = None,
        busy_timeout_ms: int = 30_000,
        synchronous: str = "FULL",
    ):
        if serializer is None:
            raise TypeError("serializer is required.")

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
        self._repository = SQLiteExecutionRepository(client, serializer)

        self._client._initialize_schema_sync()

    @property
    def repository(self) -> SQLiteExecutionRepository:
        return self._repository

    # -- SessionStore API ------------------------------------------------------

    async def begin_transaction(self) -> Transaction:
        return await _ClientTransaction(self._client).begin()

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        return await self._repository.start_execution(execution_id)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        return await self._repository.get_execution_record(execution_id, return_type)

    async def set_metadata(
        self, execution_id: ExecutionId, key: str, value: Any, value_type: type
    ) -> None:
        await self._repository.set_metadata(execution_id, key, value, value_type)

    async def get_metadata(
        self, execution_id: ExecutionId, key: str, return_type: type
    ) -> Any | None:
        return await self._repository.get_metadata(execution_id, key, return_type)
