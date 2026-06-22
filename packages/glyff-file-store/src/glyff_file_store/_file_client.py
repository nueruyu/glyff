from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, NamedTuple, Union

from .exceptions import InvalidStagedContentError

logger = logging.getLogger(__name__)

WriteCallback = Callable[[], Awaitable[bytes]]
ClearCallback = Callable[[], Coroutine[Any, Any, None]]
Content = Union[bytes, WriteCallback]

_BACKUP_SUFFIX = ".bak"
_TEMP_PREFIX = ".commit-"
_PERMISSION_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2)


class StagedOperation(NamedTuple):
    write: WriteCallback
    clear: ClearCallback | None


class FileClient:
    """A file-based data store with directory-level atomic writes.

    Each commit builds the full new session state in a sibling temp directory
    and then swaps it into place with two renames (session → backup, temp →
    session, drop backup). Either every file in that commit lands on disk or
    none does, regardless of how many files were touched, and a writer callback
    raising mid-commit leaves the on-disk session unchanged.

    Construction performs a small recovery sweep: any orphan ``.bak`` sibling
    left by a previously interrupted commit is either restored (if the session
    itself is missing) or dropped, and any orphan ``.commit-*`` temp directories
    are removed.
    """

    def __init__(self, base_dir: str | Path, session_id: str):
        if any(c in session_id for c in ("/", "\\", "..")):
            raise ValueError("session_id cannot contain path traversal elements.")
        self._session_path = Path(base_dir) / session_id
        self._recover_crashed_commit_sync()
        self._session_path.mkdir(parents=True, exist_ok=True)
        # Staged operations remain only as a compatibility layer. New store
        # code should use write/delete so each event is durable immediately.
        self._staged_ops: dict[str, StagedOperation] = {}
        self._staged_deletes: set[str] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    def _recover_crashed_commit_sync(self) -> None:
        """Clean up orphan ``.bak`` / ``.commit-*`` siblings from a prior
        crashed commit, restoring the session from a backup if the swap
        was interrupted between the two renames."""
        backup = self._session_path.with_name(self._session_path.name + _BACKUP_SUFFIX)
        if backup.exists():
            if not self._session_path.exists():
                # Crashed between rename-to-backup and rename-from-temp.
                # The backup holds the only good copy; restore it.
                self._rename_path_sync(backup, self._session_path)
            else:
                # rename-from-temp succeeded but rmtree-backup was
                # interrupted, or this is otherwise a stale backup.
                self._remove_path_if_exists_sync(backup, ignore_errors=True)

        parent = self._session_path.parent
        if parent.exists():
            temp_prefix = self._session_path.name + _TEMP_PREFIX
            for sibling in parent.iterdir():
                if sibling.name.startswith(temp_prefix) and sibling.is_dir():
                    self._remove_path_if_exists_sync(sibling, ignore_errors=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, path: str | Path) -> Path:
        return self._session_path / path

    async def read(self, path: str | Path, *, staged: bool = True) -> bytes | None:
        """Read the bytes for ``path``.

        With ``staged=True`` (the default) the read includes compatibility
        staged operations, if any. New per-event store paths should not depend
        on staged reads.
        """
        rel_str = str(path)
        if staged:
            if rel_str in self._staged_deletes:
                return None
            op = self._staged_ops.get(rel_str)
            if op is not None:
                return await op.write()
        try:
            return await asyncio.to_thread(self.resolve(path).read_bytes)
        except FileNotFoundError:
            return None

    async def write(self, path: str | Path, content: Content) -> None:
        """Atomically write ``content`` to ``path`` immediately."""
        callback = self._content_to_callback(content)
        resolved = await callback()
        rel_str = str(path)
        async with self._lock:
            await asyncio.to_thread(self._commit_to_disk_sync, {rel_str: resolved}, set())

    async def delete(self, path: str | Path) -> None:
        """Atomically delete ``path`` immediately, if it exists."""
        rel_str = str(path)
        async with self._lock:
            await asyncio.to_thread(self._commit_to_disk_sync, {}, {rel_str})

    async def stage_write(
        self,
        path: str | Path,
        content: Content,
        clear_callback: ClearCallback | None = None,
    ) -> None:
        """Stage a write of ``content`` to ``path`` for a compatibility commit."""
        op = StagedOperation(write=self._content_to_callback(content), clear=clear_callback)

        rel_str = str(path)
        async with self._lock:
            self._staged_ops[rel_str] = op
            self._staged_deletes.discard(rel_str)

    async def stage_delete(self, path: str | Path) -> None:
        rel_str = str(path)
        async with self._lock:
            self._staged_deletes.add(rel_str)
            cancelled = self._staged_ops.pop(rel_str, None)
        # Run the cancelled op's clear callback outside the lock so a
        # callback that re-enters FileClient cannot deadlock.
        if cancelled is not None and cancelled.clear is not None:
            await cancelled.clear()

    async def clear_staged(self) -> None:
        async with self._lock:
            clear_tasks = self._clear_staged_under_lock()
        if clear_tasks:
            await asyncio.gather(*clear_tasks)

    async def commit_staged(self) -> None:
        async with self._lock:
            if not self._staged_ops and not self._staged_deletes:
                return

            # Resolve all writer callbacks before touching disk. If any
            # raises, nothing has changed and the staged ops remain in
            # place for retry.
            resolved_writes: dict[str, bytes] = {}
            for rel_path, op in self._staged_ops.items():
                resolved_writes[rel_path] = await op.write()

            staged_deletes = set(self._staged_deletes)

            await asyncio.to_thread(
                self._commit_to_disk_sync, resolved_writes, staged_deletes
            )

            clear_tasks = self._clear_staged_under_lock()
        if clear_tasks:
            await asyncio.gather(*clear_tasks)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _content_to_callback(self, content: Content) -> WriteCallback:
        if isinstance(content, bytes):

            async def bytes_writer() -> bytes:
                return content

            return bytes_writer
        if callable(content):
            return content
        raise InvalidStagedContentError("content must be bytes or a callable")

    def _clear_staged_under_lock(self) -> list[Coroutine[Any, Any, None]]:
        """Clear all staged ops/deletes; return the clear callbacks so
        the caller can await them outside the lock. Caller must hold
        ``self._lock``."""
        clear_tasks: list[Coroutine[Any, Any, None]] = [
            op.clear() for op in self._staged_ops.values() if op.clear
        ]
        self._staged_ops.clear()
        self._staged_deletes.clear()
        return clear_tasks

    def _commit_to_disk_sync(
        self,
        resolved_writes: dict[str, bytes],
        staged_deletes: set[str],
    ) -> None:
        parent = self._session_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(
            tempfile.mkdtemp(dir=parent, prefix=self._session_path.name + _TEMP_PREFIX)
        )
        try:
            self._populate_temp_dir_sync(temp_dir, resolved_writes, staged_deletes)
            self._swap_temp_into_place_sync(temp_dir)
        except Exception:
            self._remove_path_if_exists_sync(temp_dir, ignore_errors=True)
            raise

    def _populate_temp_dir_sync(
        self,
        temp_dir: Path,
        resolved_writes: dict[str, bytes],
        staged_deletes: set[str],
    ) -> None:
        # Mirror the existing session into temp_dir.
        if self._session_path.exists():
            shutil.copytree(self._session_path, temp_dir, dirs_exist_ok=True)

        for rel_path in staged_deletes:
            target = temp_dir / rel_path
            if target.is_symlink() or target.is_file():
                target.unlink()
            # Directory deletes aren't part of the API.

        for rel_path, content in resolved_writes.items():
            target = temp_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

    def _swap_temp_into_place_sync(self, temp_dir: Path) -> None:
        backup = self._session_path.with_name(self._session_path.name + _BACKUP_SUFFIX)
        if backup.exists():
            # Defensive: drop any stale backup from a prior crash.
            self._remove_path_if_exists_sync(backup)

        if self._session_path.exists():
            self._rename_path_sync(self._session_path, backup)

        try:
            self._rename_path_sync(temp_dir, self._session_path)
        except BaseException:
            # Swap failed; restore the original from backup.
            if backup.exists() and not self._session_path.exists():
                self._rename_path_sync(backup, self._session_path)
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
