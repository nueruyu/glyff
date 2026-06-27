from __future__ import annotations

import json
import logging
from collections.abc import Iterable
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


async def _to_record(
    stored: dict[str, Any],
    return_type: type,
    serializer: JsonSerializer,
) -> ExecutionRecord:
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


class _FileExecution(Execution):
    def __init__(
        self,
        client: FileClient,
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
            executions = _decode(data)
            stored = executions.get(self._key)
            if stored is None:
                raise LookupError(
                    f"Execution at {self._key} not found"
                )
            if stored["status"] in ("completed", "failed"):
                raise ValueError(
                    f"Cannot complete execution at {self._key}: "
                    f"already {stored['status']}"
                )
            stored["status"] = _STATUS_NAMES[ExecutionStatus.COMPLETED]
            stored["result_json"] = result_json
            return _encode(executions)

        self._client.stage_update(_EXECUTIONS_FILE, fn)

    async def fail(self, error: str) -> None:
        def fn(data: bytes | None) -> bytes | None:
            executions = _decode(data)
            stored = executions.get(self._key)
            if stored is None:
                raise LookupError(
                    f"Execution at {self._key} not found"
                )
            if stored["status"] in ("completed", "failed"):
                raise ValueError(
                    f"Cannot fail execution at {self._key}: "
                    f"already {stored['status']}"
                )
            stored["status"] = _STATUS_NAMES[ExecutionStatus.FAILED]
            stored["error"] = error
            return _encode(executions)

        self._client.stage_update(_EXECUTIONS_FILE, fn)


class JsonFileSessionStore(SessionStore):
    """Human-readable debug SessionStore backed by a JSON file.

    All data is stored as a single JSON dict in ``executions.json``.
    Mutations are staged via ``FileClient.stage_update`` and applied
    atomically on commit.
    """

    def __init__(self, client: FileClient, serializer: JsonSerializer):
        self._client = client
        self._serializer = serializer

    async def begin_transaction(self) -> Transaction:
        return await _ClientTransaction(self._client).begin()

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        key = execution_id_to_path(execution_id)
        existing = await self.get_execution_record(execution_id, type(None))
        if existing is None:
            def fn(data: bytes | None) -> bytes | None:
                executions = _decode(data)
                if key in executions:
                    status = executions[key]["status"]
                    if status in (
                        _STATUS_NAMES[ExecutionStatus.COMPLETED],
                        _STATUS_NAMES[ExecutionStatus.FAILED],
                    ):
                        raise ValueError(
                            f"Cannot start execution {execution_id}: "
                            f"already {status}"
                        )
                executions[key] = {
                    "status": _STATUS_NAMES[ExecutionStatus.STARTED],
                    "result_json": None,
                    "error": None,
                }
                return _encode(executions)

            self._client.stage_update(_EXECUTIONS_FILE, fn)

        return _FileExecution(self._client, execution_id, self._serializer)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        key = execution_id_to_path(execution_id)
        raw = await self._client.read(_EXECUTIONS_FILE, staged=True)
        if raw is None:
            return None
        executions = _decode(raw)
        stored = executions.get(key)
        if stored is None:
            return None
        return await _to_record(stored, return_type, self._serializer)

    async def get_descendants(
        self, execution_id: ExecutionId
    ) -> list[ExecutionId]:
        prefix = execution_id_to_path(execution_id) + "/"
        raw = await self._client.read(_EXECUTIONS_FILE, staged=True)
        if raw is None:
            return []
        executions = _decode(raw)
        result: list[ExecutionId] = []
        for key in executions:
            if key.startswith(prefix):
                result.append(path_to_execution_id(key))
        return result

    async def delete_executions(
        self, execution_ids: Iterable[ExecutionId]
    ) -> None:
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
