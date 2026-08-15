from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from collections.abc import AsyncGenerator, Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from filelock import AsyncFileLock, FileLock
from glyff import DomainId
from glyff.exceptions import StoreFormatVersionError
from glyff.serialization.constants import DEFAULT_ENCODING
from glyff.store.aggregate_codec import execution_to_dict
from glyff.store.utils import execution_id_to_path
from glyff.store.workers import run_to_completion
from glyff.store.staging import (
    DeleteExecution,
    ExecutionKey,
    ExecutionMutation,
)

logger = logging.getLogger(__name__)

Executions = dict[str, dict[str, Any]]

STORE_FILE = "glyff.json"
_LOCK_FILE = ".glyff.lock"
TEMP_PREFIX = ".glyff-write-"

_FORMAT_VERSION_KEY = "format_version"
SESSIONS_KEY = "sessions"
DOMAIN_VERSIONS_KEY = "domain_versions"
EXECUTIONS_KEY = "executions"

# Windows refuses to replace a file another handle has open; the reader releases
# it in microseconds, so a short retry is enough.
_PERMISSION_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2)

T = TypeVar("T")


@dataclass(frozen=True)
class DocumentUpdate(Generic[T]):
    """An operation's answer, and whether the document needs writing back."""

    result: T
    changed: bool = True

    @classmethod
    def unchanged(cls, result: T) -> DocumentUpdate[T]:
        return cls(result, changed=False)


class FileClient:
    """The store's JSON document: committed reads, locking, atomic replacement,
    and the session claim (see the README)."""

    def __init__(self, base_dir: str | Path, *, format_version: int) -> None:
        self._base_path = Path(base_dir)
        self._base_path.mkdir(parents=True, exist_ok=True)
        self._path = self._base_path / STORE_FILE
        self._lock_path = self._base_path / _LOCK_FILE
        self._format_version = format_version
        self._lock = asyncio.Lock()
        self._file_lock = AsyncFileLock(self._lock_path)
        with self._exclusive_sync():
            self._initialize_sync()

    # -- Locking ---------------------------------------------------------------

    @contextmanager
    def _exclusive_sync(self) -> Generator[None, None, None]:
        with FileLock(self._lock_path):
            yield

    @asynccontextmanager
    async def _exclusive(self) -> AsyncGenerator[None, None]:
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
        for leftover in self._base_path.glob(TEMP_PREFIX + "*"):
            leftover.unlink(missing_ok=True)

        document = self._read_document_sync()
        stored = document.get(_FORMAT_VERSION_KEY)
        if stored is None:
            self._write_document_sync(
                {_FORMAT_VERSION_KEY: self._format_version, SESSIONS_KEY: {}}
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

        handle, temp_name = tempfile.mkstemp(dir=self._base_path, prefix=TEMP_PREFIX)
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
        sessions = document.get(SESSIONS_KEY, {})
        return sessions.get(session_id, {}).get(EXECUTIONS_KEY, {})

    # -- Read / commit ---------------------------------------------------------

    async def read_committed_executions(self, session_id: str) -> Executions:
        # No lock: a commit replaces the document rather than rewriting it, so
        # this opens either the whole old one or the whole new one.
        document = await asyncio.to_thread(self._read_document_sync)
        return dict(self._session_executions(document, session_id))

    async def commit_mutations(
        self, mutations: Mapping[ExecutionKey, ExecutionMutation]
    ) -> None:
        if not mutations:
            return

        def apply(document: dict[str, Any]) -> DocumentUpdate[None]:
            sessions = document.setdefault(SESSIONS_KEY, {})
            for key, mutation in mutations.items():
                session = sessions.setdefault(key.session_id.value, {})
                executions = session.setdefault(EXECUTIONS_KEY, {})
                path = execution_id_to_path(key.execution_id)

                if isinstance(mutation, DeleteExecution):
                    executions.pop(path, None)
                else:
                    executions[path] = execution_to_dict(
                        mutation.snapshot.to_execution()
                    )
            return DocumentUpdate(None)

        await self.update_document(apply)

    # -- The write primitive ---------------------------------------------------

    async def update_document(
        self, operation: Callable[[dict[str, Any]], DocumentUpdate[T]]
    ) -> T:
        """Reads the document, hands it to ``operation`` to change in place, and
        replaces it if the operation changed anything — with the store held, off
        the event loop.

        Every write goes through here, and one document carries every session, so
        a single replacement covers whatever ``operation`` touched.
        """
        async with self._exclusive():
            return await run_to_completion(
                lambda: self._update_document_sync(operation)
            )

    def _update_document_sync(
        self, operation: Callable[[dict[str, Any]], DocumentUpdate[T]]
    ) -> T:
        document = self._read_document_sync()
        update = operation(document)
        if update.changed:
            self._write_document_sync(document)
        return update.result

    # -- Domain versions -------------------------------------------------------

    async def claim_domain(
        self, session_id: str, domain_id: DomainId, version: str
    ) -> str:
        def claim(document: dict[str, Any]) -> DocumentUpdate[str]:
            session = document.setdefault(SESSIONS_KEY, {}).setdefault(session_id, {})
            versions = session.setdefault(DOMAIN_VERSIONS_KEY, {})
            recorded = versions.get(domain_id.value)
            if recorded is not None:
                # Rewriting a whole store to report an unchanged version would
                # re-serialize and fsync every session in it.
                return DocumentUpdate[str].unchanged(recorded)

            versions[domain_id.value] = version
            return DocumentUpdate(version)

        return await self.update_document(claim)
