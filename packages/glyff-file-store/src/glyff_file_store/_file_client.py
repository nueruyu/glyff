from __future__ import annotations

import asyncio
import contextvars
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
        if any(c in session_id for c in ("/", "\\", "..")):
            raise ValueError("session_id cannot contain path traversal elements.")
        self._session_path = Path(base_dir) / session_id
        self._recover_crashed_commit_sync()
        self._session_path.mkdir(parents=True, exist_ok=True)
        # One staged write per path; staging the same path twice in a
        # transaction replaces the earlier op (last write wins).
        self._staged_ops: dict[str, StagedOperation] = {}
        self._staged_deletes: set[str] = set()
        # Paths whose staged write callback is currently being resolved. A
        # callback that reads its own path (e.g. append semantics) must see the
        # committed file rather than recursing into its own staged op. A
        # ContextVar (rather than a task-keyed set) means any child tasks the
        # callback spawns inherit the resolving set — so the recursion guard
        # still fires across ``create_task``/``gather`` — while unrelated
        # concurrent reads keep their own context and still resolve the op.
        self._resolving: contextvars.ContextVar[frozenset[str]] = (
            contextvars.ContextVar("file_client_resolving", default=frozenset())
        )
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

    async def read(self, path: str | Path) -> bytes | None:
        """Read the bytes visible to the current transaction: a staged write
        overrides the committed file, a staged delete reads as ``None``, and
        otherwise the committed file is read from disk.

        Serving a staged write means invoking its commit-time callback. The
        result is intentionally **not** cached: a callback may close over
        mutable state that keeps changing during the transaction (e.g. a
        store that appends to an in-memory buffer and serializes it at
        commit), so each read re-resolves the current staged value and commit
        sees the latest state. Callbacks should therefore be free of
        observable side effects.

        Like the committed-disk read, this does not hold ``self._lock``: the
        staged dicts are inspected synchronously (an atomic snapshot under the
        single-threaded event loop) and the write callback is awaited without
        the lock, so an append-style callback that re-enters ``read`` cannot
        deadlock. While such a callback runs, a nested read of the *same* path
        from the same task (or a child task it spawns) falls through to the
        committed file (see ``self._resolving``); an unrelated concurrent read
        from another task still resolves the staged op.
        """
        rel_str = str(path)
        resolving = self._resolving.get()
        if rel_str in resolving:
            # This task's staged write callback is reading its own path; serve
            # the committed file so it can compute the new content (and so it
            # cannot recurse into its own staged op).
            return await self._read_committed(path)
        if rel_str in self._staged_deletes:
            return None
        op = self._staged_ops.get(rel_str)
        if op is None:
            return await self._read_committed(path)

        token = self._resolving.set(resolving | {rel_str})
        try:
            return await op.write()
        finally:
            self._resolving.reset(token)

    async def _read_committed(self, path: str | Path) -> bytes | None:
        """Read the committed file from disk, ignoring any staged state."""
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

        if isinstance(content, bytes):

            async def bytes_writer() -> bytes:
                return content

            callback = bytes_writer
        elif callable(content):
            callback = content
        else:
            raise InvalidStagedContentError("content must be bytes or a callable")
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
            # place for retry. Callbacks are resolved fresh here (results are
            # never cached) so the committed bytes reflect the latest staged
            # state, even when a callback closes over state that changed after
            # an earlier read(). Mark each path as resolving so an append-style
            # callback reading its own path sees the committed file instead of
            # recursing into its own staged op.
            resolved_writes: dict[str, bytes] = {}
            for rel_path, op in self._staged_ops.items():
                token = self._resolving.set(self._resolving.get() | {rel_path})
                try:
                    resolved_writes[rel_path] = await op.write()
                finally:
                    self._resolving.reset(token)

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
            target.write_bytes(content)

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
