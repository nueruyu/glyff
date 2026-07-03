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
from glyff.serialization.constants import DEFAULT_ENCODING, JSON_SEPARATORS
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._file_client import FileClient
from ._transaction import _ClientTransaction

_EXECUTIONS_FILE = "executions.json"

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


def _decode(raw: bytes | None) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    return json.loads(raw.decode(DEFAULT_ENCODING))


def _encode(executions: dict[str, dict[str, Any]]) -> bytes:
    return json.dumps(
        executions,
        indent=2,
        sort_keys=True,
        separators=JSON_SEPARATORS,
        ensure_ascii=False,
    ).encode(DEFAULT_ENCODING)


def _to_execution(execution_id: ExecutionId, stored: dict[str, Any]) -> Execution:
    return Execution(
        id=execution_id,
        status=_NAME_TO_STATUS[stored["status"]],
        result=_unpack_value(stored.get("result_b64")),
        error=stored.get("error") if isinstance(stored.get("error"), str) else None,
        metadata=_unpack_metadata(stored.get("metadata")),
    )


def _from_execution(execution: Execution) -> dict[str, Any]:
    return {
        "status": _STATUS_NAMES[execution.status],
        "result_b64": _pack_value(execution.result),
        "error": execution.error,
        "metadata": _pack_metadata(execution.metadata),
    }


class FileExecutionRepository(ExecutionRepository):
    """File-backed Execution aggregate repository."""

    def __init__(self, client: FileClient):
        self._client = client

    async def get(self, execution_id: ExecutionId) -> Execution | None:
        key = execution_id_to_path(execution_id)
        raw = await self._client.read(_EXECUTIONS_FILE, staged=True)
        if raw is None:
            return None
        stored = _decode(raw).get(key)
        if stored is None:
            return None
        return _to_execution(execution_id, stored)

    async def save(self, execution: Execution) -> None:
        key = execution_id_to_path(execution.id)

        def fn(data: bytes | None) -> bytes | None:
            executions = _decode(data)
            executions[key] = _from_execution(execution)
            return _encode(executions)

        self._client.stage_update(_EXECUTIONS_FILE, fn)

    async def descendants_of(self, execution_id: ExecutionId) -> list[ExecutionId]:
        prefix = execution_id_to_path(execution_id) + "/"
        raw = await self._client.read(_EXECUTIONS_FILE, staged=True)
        if raw is None:
            return []
        return [path_to_execution_id(k) for k in _decode(raw) if k.startswith(prefix)]

    async def delete_many(self, execution_ids: Iterable[ExecutionId]) -> None:
        keys = {execution_id_to_path(eid) for eid in execution_ids}
        if not keys:
            return

        def fn(data: bytes | None) -> bytes | None:
            if data is None:
                return None
            executions = _decode(data)
            for key in keys:
                executions.pop(key, None)
            return _encode(executions)

        self._client.stage_update(_EXECUTIONS_FILE, fn)


class FileTransactionProvider(TransactionProvider):
    def __init__(self, client: FileClient):
        self._client = client

    async def begin_transaction(self) -> Transaction:
        return await _ClientTransaction(self._client).begin()


class JsonFileBackend:
    def __init__(self, *, base_dir: str | Path, session_id: str):
        client = FileClient(base_dir=base_dir, session_id=session_id)
        self.executions: ExecutionRepository = FileExecutionRepository(client)
        self.transactions: TransactionProvider = FileTransactionProvider(client)
