from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from filelock import AsyncFileLock, FileLock

logger = logging.getLogger(__name__)

FileUpdate = Callable[[bytes | None], bytes | None]

# Files are staged under the session that owns them, so one store can hold many
# sessions without their paths colliding.
FileKey = tuple[str, str]


@dataclass(frozen=True)
class _Write:
    data: bytes


@dataclass(frozen=True)
class _Delete:
    pass


@dataclass(frozen=True)
class _Update:
    fn: FileUpdate


_FileOp = _Write | _Delete | _Update

# All start with a dot, which a SessionId cannot, so a session directory can
# never be mistaken for the store's own bookkeeping.
_BACKUP_PREFIX = ".bak-"
_TEMP_PREFIX = ".commit-"
_LOCK_FILE = ".glyff.lock"
_PERMISSION_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2)


def replace_atomically(target: Path, data: bytes) -> None:
    """Writes ``data`` to ``target`` so a crash leaves the old bytes, not half of
    the new ones."""
    handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=_TEMP_PREFIX)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


class _FileStagingBuffer:
    __slots__ = ("ops", "session_id")

    def __init__(self) -> None:
        self.ops: dict[FileKey, list[_FileOp]] = {}
        self.session_id: str | None = None

    def enlist(self, session_id: str) -> None:
        if self.session_id is None:
            self.session_id = session_id
        elif self.session_id != session_id:
            raise RuntimeError(
                "A file store transaction covers one session: this one already "
                f"holds writes for {self.session_id!r} and cannot also take "
                f"{session_id!r}. A commit swaps each session directory into "
                "place on its own, so spanning two would not be atomic."
            )

    def clear(self) -> None:
        self.ops.clear()
        self.session_id = None


class FileClient:
    """A file-based transactional key/value store, one directory per session.

    Each transaction stages write/delete/update operations per ``(session,
    path)``. On commit, staged operations are applied to the latest committed
    content and flushed to disk by swapping the session directory into place.

    One transaction covers one session, and a second is refused: the unit of
    atomicity here is a directory swap, and two of them are two commits however
    they are ordered. A ``Session`` only ever touches its own records, so this
    constrains nothing glyff does.

    Staging is per-transaction via ContextVar, so nested transactions each
    have their own isolated staging buffer.

    Everything that mutates or replaces store state runs under two locks: an
    ``asyncio.Lock`` for the tasks of one process, and a lock file beside the
    session directories for the processes sharing the store. Both are needed —
    the file lock is re-entrant per handle, so it does not serialize coroutines
    holding the same one.
    """

    def __init__(self, base_dir: str | Path):
        self._base_path = Path(base_dir)
        self._base_path.mkdir(parents=True, exist_ok=True)
        self._lock_path = self._base_path / _LOCK_FILE
        self._lock = asyncio.Lock()
        self._file_lock = AsyncFileLock(self._lock_path)
        with self.exclusive_sync():
            self._recover_crashed_commits_sync()
        self._current: contextvars.ContextVar[_FileStagingBuffer | None] = (
            contextvars.ContextVar("file_client_staging", default=None)
        )

    @contextmanager
    def exclusive_sync(self) -> Iterator[None]:
        """Store-wide exclusion for the synchronous paths, which run before any
        transaction exists: opening the store and recovering a crashed commit."""
        with FileLock(self._lock_path):
            yield

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Store-wide exclusion for anything that replaces committed state."""
        async with self._lock:
            async with self._file_lock:
                yield

    def _recover_crashed_commits_sync(self) -> None:
        for child in list(self._base_path.iterdir()):
            if child.name.startswith(_BACKUP_PREFIX) and child.is_dir():
                restored = child.with_name(child.name[len(_BACKUP_PREFIX) :])
                if restored.exists():
                    self._remove_path_if_exists_sync(child, ignore_errors=True)
                else:
                    self._rename_path_sync(child, restored)
            elif child.name.startswith(_TEMP_PREFIX):
                self._remove_path_if_exists_sync(child, ignore_errors=True)
            elif child.is_dir():
                # A crash between writing a replacement and renaming it leaves a
                # temporary inside the session directory it belongs to.
                for leftover in child.glob(_TEMP_PREFIX + "*"):
                    self._remove_path_if_exists_sync(leftover, ignore_errors=True)

    def resolve(self, key: FileKey) -> Path:
        session_id, path = key
        return self._base_path / session_id / path

    def resolve_store_file(self, name: str) -> Path:
        """A file belonging to the store itself rather than to a session."""
        return self._base_path / name

    def session_path(self, session_id: str) -> Path:
        return self._base_path / session_id

    # -- Staging context management -------------------------------------------

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

    # -- Staging API ----------------------------------------------------------

    def stage_write(self, key: FileKey, data: bytes) -> None:
        self._stage(key, _Write(data))

    def stage_delete(self, key: FileKey) -> None:
        self._stage(key, _Delete())

    def stage_update(self, key: FileKey, fn: FileUpdate) -> None:
        self._stage(key, _Update(fn))

    def _stage(self, key: FileKey, op: _FileOp) -> None:
        staging = self._require_staging()
        staging.enlist(key[0])
        staging.ops.setdefault(key, []).append(op)

    async def clear_staged(self) -> None:
        self._require_staging().clear()

    # -- Read -----------------------------------------------------------------

    async def read(self, key: FileKey, *, staged: bool = True) -> bytes | None:
        # Under the lock: a swap renames the session directory away and back, so
        # an unguarded read can fall into the gap and report a recorded
        # execution as missing. Only the committed snapshot needs guarding —
        # staged ops belong to the caller's own transaction.
        async with self.exclusive():
            data = await asyncio.to_thread(self._read_committed_sync, key)

        if staged:
            staging = self._current.get()
            if staging is not None:
                data = self._apply_ops(data, staging.ops.get(key, []))

        return data

    def _read_committed_sync(self, key: FileKey) -> bytes | None:
        try:
            return self.resolve(key).read_bytes()
        except FileNotFoundError:
            return None

    async def update_committed(self, key: FileKey, fn: FileUpdate) -> bytes | None:
        """Applies ``fn`` to the committed file and writes the result back.

        Outside any transaction, under the same lock as commit, so a caller that
        must read and write in one step is not interleaved with a directory swap.
        """
        async with self.exclusive():
            return await asyncio.to_thread(self._update_committed_sync, key, fn)

    def _update_committed_sync(self, key: FileKey, fn: FileUpdate) -> bytes | None:
        current = self._read_committed_sync(key)
        updated = fn(current)
        if updated is not None and updated != current:
            target = self.resolve(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            replace_atomically(target, updated)
        return updated

    @staticmethod
    def _apply_ops(data: bytes | None, ops: list[_FileOp]) -> bytes | None:
        current = data
        for op in ops:
            if isinstance(op, _Write):
                current = op.data
            elif isinstance(op, _Delete):
                current = None
            elif isinstance(op, _Update):
                current = op.fn(current)
            else:
                raise TypeError(f"Unknown file op: {op!r}")
        return current

    # -- Commit ---------------------------------------------------------------

    async def commit_staged(self) -> None:
        staging = self._require_staging()

        if not staging.ops:
            return

        session_id = staging.session_id
        assert session_id is not None  # a non-empty buffer has been enlisted

        async with self.exclusive():
            resolved_writes: dict[str, bytes] = {}
            resolved_deletes: set[str] = set()

            for key, ops in staging.ops.items():
                path = key[1]
                final = self._apply_ops(self._read_committed_sync(key), ops)

                if final is None:
                    resolved_deletes.add(path)
                    resolved_writes.pop(path, None)
                else:
                    resolved_writes[path] = final
                    resolved_deletes.discard(path)

            await asyncio.to_thread(
                self._commit_to_disk_sync,
                session_id,
                resolved_writes,
                resolved_deletes,
            )

            staging.clear()

    # -- Disk helpers ---------------------------------------------------------

    def _commit_to_disk_sync(
        self,
        session_id: str,
        resolved_writes: dict[str, bytes],
        staged_deletes: set[str],
    ) -> None:
        session_path = self.session_path(session_id)
        self._base_path.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(dir=self._base_path, prefix=_TEMP_PREFIX))
        try:
            self._populate_temp_dir_sync(
                session_path, temp_dir, resolved_writes, staged_deletes
            )
            self._swap_temp_into_place_sync(session_path, temp_dir)
        except Exception:
            self._remove_path_if_exists_sync(temp_dir, ignore_errors=True)
            raise

    def _populate_temp_dir_sync(
        self,
        session_path: Path,
        temp_dir: Path,
        resolved_writes: dict[str, bytes],
        staged_deletes: set[str],
    ) -> None:
        if session_path.exists():
            shutil.copytree(session_path, temp_dir, dirs_exist_ok=True)

        for rel_path in staged_deletes:
            target = temp_dir / rel_path
            if target.is_symlink() or target.is_file():
                target.unlink()

        for rel_path, content in resolved_writes.items():
            target = temp_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def _swap_temp_into_place_sync(self, session_path: Path, temp_dir: Path) -> None:
        backup = session_path.with_name(_BACKUP_PREFIX + session_path.name)
        if backup.exists():
            self._remove_path_if_exists_sync(backup)

        if session_path.exists():
            self._rename_path_sync(session_path, backup)

        try:
            self._rename_path_sync(temp_dir, session_path)
        except BaseException:
            if backup.exists() and not session_path.exists():
                self._rename_path_sync(backup, session_path)
            raise

        if backup.exists():
            self._remove_path_if_exists_sync(backup, ignore_errors=True)

    def _remove_path_if_exists_sync(
        self, path: Path, *, ignore_errors: bool = False
    ) -> None:
        try:
            if not path.exists():
                return
            if path.is_dir():
                self._retry_permission_error_sync(
                    lambda: shutil.rmtree(path),
                    f"remove directory {path}",
                )
            else:
                self._retry_permission_error_sync(
                    path.unlink,
                    f"remove file {path}",
                )
        except FileNotFoundError:
            return
        except OSError as e:
            if not ignore_errors:
                raise
            logger.warning("Could not remove %s: %s", path, e)

    def _rename_path_sync(self, source: Path, target: Path) -> None:
        self._retry_permission_error_sync(
            lambda: os.rename(source, target),
            f"rename {source} to {target}",
        )

    def _retry_permission_error_sync(
        self, operation: Callable[[], Any], description: str
    ) -> Any:
        for delay in (*_PERMISSION_RETRY_DELAYS, None):
            try:
                return operation()
            except PermissionError:
                if delay is None:
                    raise
                logger.debug(
                    "Retrying file store operation after PermissionError: %s",
                    description,
                )
                time.sleep(delay)
