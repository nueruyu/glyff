from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from filelock import AsyncFileLock, FileLock
from glyff.exceptions import StoreFormatVersionError
from glyff.serialization.constants import DEFAULT_ENCODING

logger = logging.getLogger(__name__)

Executions = dict[str, dict[str, Any]]
SessionUpdate = Callable[[Executions], Executions]

_STORE_FILE = "glyff.json"
_LOCK_FILE = ".glyff.lock"
_TEMP_PREFIX = ".glyff-write-"

_FORMAT_VERSION_KEY = "format_version"
_SESSIONS_KEY = "sessions"
_APP_VERSION_KEY = "app_version"
_EXECUTIONS_KEY = "executions"

# Windows refuses to replace a file another handle has open; the reader releases
# it in microseconds, so a short retry is enough.
_PERMISSION_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2)


def _apply(executions: Executions, updates: list[SessionUpdate]) -> Executions:
    for update in updates:
        executions = update(executions)
    return executions


class _FileStagingBuffer:
    __slots__ = ("updates",)

    def __init__(self) -> None:
        self.updates: dict[str, list[SessionUpdate]] = {}

    def clear(self) -> None:
        self.updates.clear()


class FileClient:
    """Transactional access to the store's JSON document (see the README)."""

    def __init__(self, base_dir: str | Path, *, format_version: int) -> None:
        self._base_path = Path(base_dir)
        self._base_path.mkdir(parents=True, exist_ok=True)
        self._path = self._base_path / _STORE_FILE
        self._lock_path = self._base_path / _LOCK_FILE
        self._format_version = format_version
        self._lock = asyncio.Lock()
        self._file_lock = AsyncFileLock(self._lock_path)
        self._current: contextvars.ContextVar[_FileStagingBuffer | None] = (
            contextvars.ContextVar("file_client_staging", default=None)
        )
        with self._exclusive_sync():
            self._initialize_sync()

    # -- Locking ---------------------------------------------------------------

    @contextmanager
    def _exclusive_sync(self) -> Iterator[None]:
        with FileLock(self._lock_path):
            yield

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Store-wide exclusion for a read-modify-write.

        Both locks are needed: the file lock keeps other processes out, and the
        ``asyncio.Lock`` keeps this process's own tasks out, because a file lock
        is re-entrant per handle and so does not serialize coroutines sharing
        one.
        """
        async with self._lock:
            async with self._file_lock:
                yield

    # -- The document ----------------------------------------------------------

    def _initialize_sync(self) -> None:
        # A crash can only strand a temporary: the document itself is replaced
        # whole, never written in place.
        for leftover in self._base_path.glob(_TEMP_PREFIX + "*"):
            leftover.unlink(missing_ok=True)

        document = self._read_document_sync()
        stored = document.get(_FORMAT_VERSION_KEY)
        if stored is None:
            self._write_document_sync(
                {_FORMAT_VERSION_KEY: self._format_version, _SESSIONS_KEY: {}}
            )
        elif stored != self._format_version:
            raise StoreFormatVersionError(
                f"File store at {self._base_path} has format version {stored!r}, "
                f"but this build of glyff writes version {self._format_version}. "
                "Refusing to open it."
            )

    def _read_document_sync(self) -> dict[str, Any]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return {}
        return json.loads(raw.decode(DEFAULT_ENCODING))

    def _write_document_sync(self, document: dict[str, Any]) -> None:
        data = json.dumps(
            document, indent=2, sort_keys=True, ensure_ascii=False
        ).encode(DEFAULT_ENCODING)

        handle, temp_name = tempfile.mkstemp(dir=self._base_path, prefix=_TEMP_PREFIX)
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            self._replace_sync(temp_name, self._path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def _replace_sync(self, source: str, target: Path) -> None:
        for delay in (*_PERMISSION_RETRY_DELAYS, None):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if delay is None:
                    raise
                logger.debug("Retrying store replacement after PermissionError.")
                time.sleep(delay)

    @staticmethod
    def _session_executions(document: dict[str, Any], session_id: str) -> Executions:
        sessions = document.get(_SESSIONS_KEY, {})
        return sessions.get(session_id, {}).get(_EXECUTIONS_KEY, {})

    # -- Staging ---------------------------------------------------------------

    def begin_staging(self) -> tuple[contextvars.Token, _FileStagingBuffer]:
        staging = _FileStagingBuffer()
        token = self._current.set(staging)
        return token, staging

    def end_staging(self, token: contextvars.Token) -> None:
        self._current.reset(token)

    def _require_staging(self) -> _FileStagingBuffer:
        staging = self._current.get()
        if staging is None:
            raise RuntimeError("FileClient write attempted outside a transaction.")
        return staging

    def _require_current_staging(self, expected: _FileStagingBuffer) -> None:
        if self._current.get() is not expected:
            raise RuntimeError("Transaction closed out of order.")

    def stage_executions(self, session_id: str, update: SessionUpdate) -> None:
        staging = self._require_staging()
        staging.updates.setdefault(session_id, []).append(update)

    async def clear_staged(self) -> None:
        self._require_staging().clear()

    # -- Read / commit ---------------------------------------------------------

    async def read_executions(
        self, session_id: str, *, staged: bool = True
    ) -> Executions:
        # No lock: a commit replaces the document rather than rewriting it, so
        # this opens either the whole old one or the whole new one.
        document = await asyncio.to_thread(self._read_document_sync)
        executions = self._session_executions(document, session_id)

        if staged:
            staging = self._current.get()
            if staging is not None:
                executions = _apply(executions, staging.updates.get(session_id, []))

        return executions

    async def commit_staged(self) -> None:
        staging = self._require_staging()

        if not staging.updates:
            return

        async with self.exclusive():
            await asyncio.to_thread(self._commit_sync, dict(staging.updates))

        staging.clear()

    def _commit_sync(self, staged: dict[str, list[SessionUpdate]]) -> None:
        document = self._read_document_sync()
        sessions = document.setdefault(_SESSIONS_KEY, {})
        for session_id, updates in staged.items():
            session = sessions.setdefault(session_id, {})
            session[_EXECUTIONS_KEY] = _apply(session.get(_EXECUTIONS_KEY, {}), updates)
        self._write_document_sync(document)

    # -- Application version ---------------------------------------------------

    async def claim_session(self, session_id: str, app_version: str) -> str:
        async with self.exclusive():
            return await asyncio.to_thread(
                self._claim_session_sync, session_id, app_version
            )

    def _claim_session_sync(self, session_id: str, app_version: str) -> str:
        document = self._read_document_sync()
        sessions = document.setdefault(_SESSIONS_KEY, {})
        session = sessions.setdefault(session_id, {})

        recorded = session.get(_APP_VERSION_KEY)
        if recorded is not None:
            return recorded

        session[_APP_VERSION_KEY] = app_version
        self._write_document_sync(document)
        return app_version
