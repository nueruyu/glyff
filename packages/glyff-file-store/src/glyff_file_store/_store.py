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
from glyff.serialization.constants import DEFAULT_ENCODING, JSON_SEPARATORS
from glyff.store.aggregate_codec import execution_from_dict, execution_to_dict
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._file_client import FileClient
from ._transaction import _ClientTransaction

_EXECUTIONS_FILE = "executions.json"


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
        return execution_from_dict(execution_id, stored)

    async def save(self, execution: Execution) -> None:
        key = execution_id_to_path(execution.id)

        def fn(data: bytes | None) -> bytes | None:
            executions = _decode(data)
            executions[key] = execution_to_dict(execution)
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
