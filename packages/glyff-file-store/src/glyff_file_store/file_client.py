from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, NamedTuple, Union

WriteCallback = Callable[[], Awaitable[bytes]]
ClearCallback = Callable[[], Coroutine[Any, Any, None]]
Content = Union[bytes, WriteCallback]

_BACKUP_SUFFIX = ".bak"
_TEMP_PREFIX = ".commit-"


class StagedOperation(NamedTuple):
    write: WriteCallback
    clear: ClearCallback | None


class FileClient:
    """A file-based data store with directory-level transactional staging.

    Each ``commit_staged`` builds the full new session state in a sibling
    temp directory and then swaps it into place with two renames (session
    → backup, temp → session, drop backup). Either every staged op lands
    on disk or none does, regardless of how many files were touched, and
    a writer callback raising mid-commit leaves the on-disk session
    unchanged.

    Construction performs a small recovery sweep: any orphan ``.bak``
    sibling left by a previously interrupted commit is either restored
    (if the session itself is missing) or dropped, and any orphan
    ``.commit-*`` temp directories are removed.
    """

    def __init__(self, base_dir: str | Path, session_id: str):
        self._session_path = Path(base_dir) / session_id
        self._recover_crashed_commit_sync()
        self._session_path.mkdir(parents=True, exist_ok=True)
        # One staged write per path; staging the same path twice in a
        # transaction replaces the earlier op (last write wins).
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
                os.rename(backup, self._session_path)
            else:
                # rename-from-temp succeeded but rmtree-backup was
                # interrupted, or this is otherwise a stale backup.
                if backup.is_dir():
                    shutil.rmtree(backup, ignore_errors=True)
                else:
                    try:
                        backup.unlink()
                    except OSError:
                        pass

        parent = self._session_path.parent
        if parent.exists():
            temp_prefix = self._session_path.name + _TEMP_PREFIX
            for sibling in parent.iterdir():
                if sibling.name.startswith(temp_prefix) and sibling.is_dir():
                    shutil.rmtree(sibling, ignore_errors=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, path: str | Path) -> Path:
        return self._session_path / path

    async def read(self, path: str | Path) -> bytes | None:
        try:
            return await asyncio.to_thread(self.resolve(path).read_bytes)
        except FileNotFoundError:
            return None

    async def stage_write(
        self,
        path: str | Path,
        content: Content,
        clear_callback: ClearCallback | None = None,
    ) -> None:
        """Stage a write of ``content`` to ``path`` for the next commit.

        ``content`` is either raw bytes or an async callback that produces
        the bytes at commit time. Callers needing append semantics can
        implement them in a callback that reads the existing file content
        and concatenates the new data.

        Staging the same path twice in one transaction replaces the
        earlier op; the displaced op's ``clear_callback`` is not invoked.
        """

        async def bytes_writer() -> bytes:
            return content if isinstance(content, bytes) else b""

        callback = content if callable(content) else bytes_writer
        op = StagedOperation(write=callback, clear=clear_callback)

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

            # Clear under the same lock so a concurrent stage_* between
            # the disk swap and the clear cannot have its work silently
            # discarded. Clear callbacks themselves are awaited outside
            # the lock so a callback that re-enters FileClient cannot
            # deadlock.
            clear_tasks = self._clear_staged_under_lock()
        if clear_tasks:
            await asyncio.gather(*clear_tasks)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
        except BaseException:
            shutil.rmtree(temp_dir, ignore_errors=True)
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
            target.write_bytes(content)

    def _swap_temp_into_place_sync(self, temp_dir: Path) -> None:
        backup = self._session_path.with_name(self._session_path.name + _BACKUP_SUFFIX)
        if backup.exists():
            # Defensive: drop any stale backup from a prior crash.
            if backup.is_dir():
                shutil.rmtree(backup)
            else:
                backup.unlink()

        if self._session_path.exists():
            os.rename(self._session_path, backup)

        try:
            os.rename(temp_dir, self._session_path)
        except BaseException:
            # Swap failed; restore the original from backup.
            if backup.exists() and not self._session_path.exists():
                os.rename(backup, self._session_path)
            raise

        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
