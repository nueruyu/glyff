from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from glyff import (
    AppVersionStore,
    Execution,
    ExecutionId,
    ExecutionRepository,
    ExecutionStatus,
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

# Holds both versions the store carries: glyff's own format version and the
# application's, written by the session that claimed the directory.
_FORMAT_FILE = "glyff_format.json"
_FORMAT_VERSION_KEY = "format_version"
_APP_VERSION_KEY = "app_version"

# Bump when the stored layout changes.
FORMAT_VERSION = 1


def _encode_marker(marker: dict[str, Any]) -> bytes:
    return json.dumps(marker, sort_keys=True).encode(DEFAULT_ENCODING)


def _initialize_format_sync(client: FileClient) -> None:
    # No marker means a session glyff has never stamped, which it adopts as current.
    marker = client.resolve(_FORMAT_FILE)
    try:
        raw = marker.read_bytes()
    except FileNotFoundError:
        raw = None

    if raw is None:
        marker.write_bytes(_encode_marker({_FORMAT_VERSION_KEY: FORMAT_VERSION}))
        return

    stored = json.loads(raw.decode(DEFAULT_ENCODING)).get(_FORMAT_VERSION_KEY)
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

    async def executions(
        self,
        *,
        status: ExecutionStatus | None = None,
        under: ExecutionId | None = None,
    ) -> AsyncIterator[Execution]:
        prefix = execution_id_to_path(under) + "/" if under is not None else ""
        raw = await self._client.read(_EXECUTIONS_FILE, staged=True)
        if raw is None:
            return
        for path, stored in sorted(_decode(raw).items()):
            if not path.startswith(prefix):
                continue
            execution = execution_from_dict(path_to_execution_id(path), stored)
            if status in (None, execution.status):
                yield execution

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


class FileAppVersionStore(AppVersionStore):
    """Keeps the application version in the same marker as the format version.

    Written through the staging buffer, so a migration's rewritten records and
    the version they were rewritten to land in one directory swap.
    """

    def __init__(self, client: FileClient):
        self._client = client

    async def read(self) -> str | None:
        raw = await self._client.read(_FORMAT_FILE, staged=True)
        if raw is None:
            return None
        return json.loads(raw.decode(DEFAULT_ENCODING)).get(_APP_VERSION_KEY)

    async def write(self, app_version: str) -> None:
        def fn(data: bytes | None) -> bytes | None:
            marker = json.loads(data.decode(DEFAULT_ENCODING)) if data else {}
            marker[_APP_VERSION_KEY] = app_version
            return _encode_marker(marker)

        self._client.stage_update(_FORMAT_FILE, fn)


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
        self.app_version: AppVersionStore | None = FileAppVersionStore(client)
