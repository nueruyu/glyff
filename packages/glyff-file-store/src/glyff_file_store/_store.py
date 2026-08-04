from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRepository,
    ExecutionStatus,
    SessionId,
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

# The store's own format version lives beside the session directories; each
# session's application version lives inside its own.
_FORMAT_FILE = "glyff_format.json"
_SESSION_FILE = "session.json"
_FORMAT_VERSION_KEY = "format_version"
_APP_VERSION_KEY = "app_version"

# Bump when the stored layout changes.
FORMAT_VERSION = 1


def _encode_marker(marker: dict[str, Any]) -> bytes:
    return json.dumps(marker, sort_keys=True).encode(DEFAULT_ENCODING)


def _decode_marker(raw: bytes | None) -> dict[str, Any]:
    return json.loads(raw.decode(DEFAULT_ENCODING)) if raw else {}


def _read_app_version(raw: bytes | None) -> str | None:
    return _decode_marker(raw).get(_APP_VERSION_KEY)


def _initialize_format_sync(client: FileClient) -> None:
    # No marker means a store glyff has never stamped, which it adopts as current.
    marker = client.resolve_store_file(_FORMAT_FILE)
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
            f"File store at {marker.parent} has format version {stored!r}, "
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

    @staticmethod
    def _key(session_id: SessionId) -> tuple[str, str]:
        return (session_id.value, _EXECUTIONS_FILE)

    async def get(
        self, session_id: SessionId, execution_id: ExecutionId
    ) -> Execution | None:
        path = execution_id_to_path(execution_id)
        raw = await self._client.read(self._key(session_id), staged=True)
        if raw is None:
            return None
        stored = _decode(raw).get(path)
        if stored is None:
            return None
        return execution_from_dict(execution_id, stored)

    async def save(self, session_id: SessionId, execution: Execution) -> None:
        path = execution_id_to_path(execution.id)

        def fn(data: bytes | None) -> bytes | None:
            executions = _decode(data)
            executions[path] = execution_to_dict(execution)
            return _encode(executions)

        self._client.stage_update(self._key(session_id), fn)

    async def executions(
        self,
        session_id: SessionId,
        *,
        status: ExecutionStatus | None = None,
        under: ExecutionId | None = None,
    ) -> AsyncIterator[Execution]:
        prefix = execution_id_to_path(under) + "/" if under is not None else ""
        raw = await self._client.read(self._key(session_id), staged=True)
        if raw is None:
            return
        for path, stored in sorted(_decode(raw).items()):
            if not path.startswith(prefix):
                continue
            execution = execution_from_dict(path_to_execution_id(path), stored)
            if status in (None, execution.status):
                yield execution

    async def delete_many(
        self, session_id: SessionId, execution_ids: Iterable[ExecutionId]
    ) -> None:
        paths = {execution_id_to_path(eid) for eid in execution_ids}
        if not paths:
            return

        def fn(data: bytes | None) -> bytes | None:
            if data is None:
                return None
            executions = _decode(data)
            for path in paths:
                executions.pop(path, None)
            return _encode(executions)

        self._client.stage_update(self._key(session_id), fn)


class FileTransactionProvider(TransactionProvider):
    def __init__(self, client: FileClient):
        self._client = client

    async def begin_transaction(self) -> Transaction:
        return await _ClientTransaction(self._client).begin()


class JsonFileBackend:
    """A file-backed backend for glyff, intended for debugging and inspection.

    This backend stores the entire execution history for a session in a single,
    pretty-printed JSON file under ``<base_dir>/<session_id>/``, and holds as
    many sessions as it is asked for. It requires a serializer that produces
    UTF-8 JSON text bytes, such as JsonSerializer or PydanticSerializer, because
    execution results and metadata are stored as embedded JSON values.

    The entire state is loaded into memory on startup and rewritten atomically
    on each commit, making it unsuitable for high-throughput or large-scale use.
    """

    def __init__(self, *, base_dir: str | Path):
        client = FileClient(base_dir=base_dir)
        _initialize_format_sync(client)
        self._client = client
        self.repository: ExecutionRepository = FileExecutionRepository(client)
        self.transaction_provider: TransactionProvider = FileTransactionProvider(client)

    async def claim_session(
        self, session_id: SessionId, app_version: str | None
    ) -> str | None:
        def fn(data: bytes | None) -> bytes | None:
            marker = _decode_marker(data)
            if marker.get(_APP_VERSION_KEY) is None:
                marker[_APP_VERSION_KEY] = app_version
            return _encode_marker(marker)

        # Read and write in one step, under the lock a commit also takes, so two
        # processes cannot both find the session unclaimed.
        return _read_app_version(
            await self._client.update_committed((session_id.value, _SESSION_FILE), fn)
        )
