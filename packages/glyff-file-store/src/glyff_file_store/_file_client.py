from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, NamedTuple, Union

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
    """A file-based data store with directory-level transactional staging.

    Commits build a replacement session directory and swap it into place
    atomically, with startup recovery for interrupted swaps.
    """

    def __init__(self, base_dir: str | Path, session_id: str):
        if any(c in session_id for c in ("/", "\\", "..")):
            raise ValueError("session_id cannot contain path traversal elements.")
        self._session_path = Path(base_dir) / session_id
        self._recover_crashed_commit_sync()
        self._session_path.mkdir(parents=True, exist_ok=True)
        self._staged_ops: dict[str, StagedOperation] = {}
        self._staged_deletes: set[str] = set()
        self._lock = asyncio.Lock()

    def _recover_crashed_commit_sync(self) -> None:
        """Clean up orphan ``.bak`` / ``.commit-*`` siblings."""
        backup = self._session_path.with_name(self._session_path.name + _BACKUP_SUFFIX)
        if backup.exists():
            if not self._session_path.exists():
                self._rename_path_sync(backup, self._session_path)
            else:
                self._remove_path_if_exists_sync(backup, ignore_errors=True)

        parent = self._session_path.parent
        if parent.exists():
            temp_prefix = self._session_path.name + _TEMP_PREFIX
            for sibling in parent.iterdir():
                if sibling.name.startswith(temp_prefix) and sibling.is_dir():
                    self._remove_path_if_exists_sync(sibling, ignore_errors=True)

    def resolve(self, path: str | Path) -> Path:
        return self._session_path / path

    async def read(self, path: str | Path, *, staged: bool = True) -> bytes | None:
        """Read the bytes for ``path``.

        With ``staged=True`` (the default) the read is transaction-aware: a
        staged write overrides the committed file and a staged delete reads
        as ``None``. With ``staged=False`` only committed state is read.
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

    async def stage_write(
        self,
        path: str | Path,
        content: Content,
        clear_callback: ClearCallback | None = None,
    ) -> None:
        """Stage a write of ``content`` to ``path`` for the next commit.

        ``content`` is either raw bytes or an async callback that produces
        the bytes at commit time.
        """

        if isinstance(content, bytes):

            async def bytes_writer() -> bytes:
                return content

            callback = bytes_writer
        else:
            callback = content
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

    def _clear_staged_under_lock(self) -> list[Coroutine[Any, Any, None]]:
        """Clear staged ops/deletes and return clear callbacks."""
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
        if self._session_path.exists():
            shutil.copytree(self._session_path, temp_dir, dirs_exist_ok=True)

        for rel_path in staged_deletes:
            target = temp_dir / rel_path
            if target.is_symlink() or target.is_file():
                target.unlink()

        for rel_path, content in resolved_writes.items():
            target = temp_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def _swap_temp_into_place_sync(self, temp_dir: Path) -> None:
        backup = self._session_path.with_name(self._session_path.name + _BACKUP_SUFFIX)
        if backup.exists():
            self._remove_path_if_exists_sync(backup)

        if self._session_path.exists():
            self._rename_path_sync(self._session_path, backup)

        try:
            self._rename_path_sync(temp_dir, self._session_path)
        except BaseException:
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
