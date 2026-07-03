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
from glyff.serialization.constants import DEFAULT_ENCODING, JSON_SEPARATORS
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._file_client import FileClient
from ._transaction import _ClientTransaction

logger = logging.getLogger(__name__)

_EXECUTIONS_FILE = "executions.json"

_STATUS_NAMES = {
    ExecutionStatus.STARTED: "started",
    ExecutionStatus.COMPLETED: "completed",
    ExecutionStatus.FAILED: "failed",
}
_NAME_TO_STATUS = {v: k for k, v in _STATUS_NAMES.items()}


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


def _from_execution(execution: Execution) -> dict[str, Any]:
    return {
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


class FileExecutionRepository(SessionStore):
    """File-backed Execution aggregate repository."""

    def __init__(
        self,
        serializer: Serializer | None = None,
        *,
        base_dir: str | Path | None = None,
        session_id: str | None = None,
        client: FileClient | None = None,
    ):
        if client is None:
            if base_dir is None or session_id is None:
                raise TypeError("base_dir and session_id (or client) are required.")
            client = FileClient(base_dir=base_dir, session_id=session_id)
        elif base_dir is not None or session_id is not None:
            raise TypeError("Pass base_dir/session_id or client, not both.")

        self._client = client
        self.serializer = serializer

    async def begin_transaction(self) -> Transaction:
        return await _ClientTransaction(self._client).begin()

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


JsonFileSessionStore = FileExecutionRepository
