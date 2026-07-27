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
from glyff.exceptions import StoreFormatVersionError
from glyff.serialization.constants import DEFAULT_ENCODING, JSON_SEPARATORS
from glyff.store.aggregate_codec import execution_from_dict, execution_to_dict
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._file_client import FileClient
from ._transaction import _ClientTransaction

_EXECUTIONS_FILE = "executions.json"

# On-disk format version, recorded in a marker file beside the session's
# executions. Bump this when the stored layout changes; opening a session
# stamped with any other version raises StoreFormatVersionError.
_FORMAT_FILE = "glyff_format.json"
FORMAT_VERSION = 1


def _initialize_format_sync(client: FileClient) -> None:
    # No marker means a new or pre-versioning session, whose layout is the
    # current version, so stamp it; any other version is refused.
    marker = client.resolve(_FORMAT_FILE)
    try:
        raw = marker.read_bytes()
    except FileNotFoundError:
        raw = None

    if raw is None:
        marker.write_bytes(
            json.dumps({"format_version": FORMAT_VERSION}).encode(DEFAULT_ENCODING)
        )
        return

    stored = json.loads(raw.decode(DEFAULT_ENCODING)).get("format_version")
    if stored != FORMAT_VERSION:
        raise StoreFormatVersionError(
            f"File store session at {marker.parent} has format version {stored!r}, "
            f"but this build of glyff writes version {FORMAT_VERSION}. "
            "Refusing to open it."
        )


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
    """A file-backed backend for glyff, intended for debugging and inspection.

    This backend stores the entire execution history for a session in a single,
    pretty-printed JSON file. It requires a serializer that produces
    UTF-8 JSON text bytes, such as JsonSerializer or PydanticSerializer, because
    execution results and metadata are stored as embedded JSON values.

    The entire state is loaded into memory on startup and rewritten atomically
    on each commit, making it unsuitable for high-throughput or large-scale use.
    """

    def __init__(self, *, base_dir: str | Path, session_id: str):
        client = FileClient(base_dir=base_dir, session_id=session_id)
        _initialize_format_sync(client)
        self.repository: ExecutionRepository = FileExecutionRepository(client)
        self.transaction_provider: TransactionProvider = FileTransactionProvider(client)
