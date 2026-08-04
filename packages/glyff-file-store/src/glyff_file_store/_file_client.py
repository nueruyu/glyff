from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FileUpdate = Callable[[bytes | None], bytes | None]


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

_BACKUP_SUFFIX = ".bak"
_TEMP_PREFIX = ".commit-"
_PERMISSION_RETRY_DELAYS = (0.01, 0.025, 0.05, 0.1, 0.2)


class _FileStagingBuffer:
    __slots__ = ("ops",)

    def __init__(self) -> None:
        self.ops: dict[str, list[_FileOp]] = {}

    def clear(self) -> None:
        self.ops.clear()


class FileClient:
    """A file-based transactional key/value store.

    Each transaction stages write/delete/update operations per path.
    On commit, staged operations are applied to the latest committed file
    content and flushed to disk atomically via directory swap.

    Staging is per-transaction via ContextVar, so nested transactions each
    have their own isolated staging buffer.
    """

    def __init__(self, base_dir: str | Path, session_id: str):
        if any(c in session_id for c in ("/", "\\", "..")):
            raise ValueError("session_id cannot contain path traversal elements.")
        self._session_path = Path(base_dir) / session_id
        self._recover_crashed_commit_sync()
        self._session_path.mkdir(parents=True, exist_ok=True)
        self._current: contextvars.ContextVar[_FileStagingBuffer | None] = (
            contextvars.ContextVar("file_client_staging", default=None)
        )
        self._lock = asyncio.Lock()

    def _recover_crashed_commit_sync(self) -> None:
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

    def stage_write(self, path: str | Path, data: bytes) -> None:
        rel = str(path)
        staging = self._require_staging()
        staging.ops.setdefault(rel, []).append(_Write(data))

    def stage_delete(self, path: str | Path) -> None:
        rel = str(path)
        staging = self._require_staging()
        staging.ops.setdefault(rel, []).append(_Delete())

    def stage_update(self, path: str | Path, fn: FileUpdate) -> None:
        rel = str(path)
        staging = self._require_staging()
        staging.ops.setdefault(rel, []).append(_Update(fn))

    async def clear_staged(self) -> None:
        self._require_staging().clear()

    # -- Read / list_keys -----------------------------------------------------

    async def read(self, path: str | Path, *, staged: bool = True) -> bytes | None:
        rel = str(path)
        data = await asyncio.to_thread(self._read_committed_sync, rel)

        if staged:
            staging = self._current.get()
            if staging is not None:
                data = self._apply_ops(data, staging.ops.get(rel, []))

        return data

    def _read_committed_sync(self, path: str) -> bytes | None:
        try:
            return self.resolve(path).read_bytes()
        except FileNotFoundError:
            return None

    async def update_committed(self, path: str | Path, fn: FileUpdate) -> bytes | None:
        """Applies ``fn`` to the committed file and writes the result back.

        Outside any transaction, under the same lock as commit, so a caller that
        must read and write in one step is not interleaved with a directory swap.
        """
        rel = str(path)
        async with self._lock:
            return await asyncio.to_thread(self._update_committed_sync, rel, fn)

    def _update_committed_sync(self, path: str, fn: FileUpdate) -> bytes | None:
        current = self._read_committed_sync(path)
        updated = fn(current)
        if updated is not None and updated != current:
            target = self.resolve(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(updated)
        return updated

    async def list_keys(self, prefix: str = "", *, staged: bool = True) -> set[str]:
        base = await asyncio.to_thread(self._list_committed_keys_sync, prefix)

        if staged:
            staging = self._current.get()
            if staging is not None:
                for key, ops in staging.ops.items():
                    if not key.startswith(prefix):
                        continue
                    final = self._apply_ops(self._read_committed_sync(key), ops)
                    if final is None:
                        base.discard(key)
                    else:
                        base.add(key)

        return base

    def _list_committed_keys_sync(self, prefix: str = "") -> set[str]:
        if not self._session_path.exists():
            return set()
        keys: set[str] = set()
        for path in self._session_path.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self._session_path).as_posix()
            if rel.startswith(prefix):
                keys.add(rel)
        return keys

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

        async with self._lock:
            resolved_writes: dict[str, bytes] = {}
            resolved_deletes: set[str] = set()

            for path, ops in staging.ops.items():
                base = self._read_committed_sync(path)
                final = self._apply_ops(base, ops)

                if final is None:
                    resolved_deletes.add(path)
                    resolved_writes.pop(path, None)
                else:
                    resolved_writes[path] = final
                    resolved_deletes.discard(path)

            await asyncio.to_thread(
                self._commit_to_disk_sync,
                resolved_writes,
                resolved_deletes,
            )

            staging.clear()

    # -- Disk helpers ---------------------------------------------------------

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
