from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, NamedTuple, Union

WriteCallback = Callable[[], Awaitable[bytes]]
ClearCallback = Callable[[], Coroutine[Any, Any, None]]
Content = Union[bytes, WriteCallback]


class StagedOperation(NamedTuple):
    write: WriteCallback
    clear: ClearCallback | None


class FileClient:
    """A low-level file-based data store with transactional capabilities."""

    def __init__(self, base_dir: str | Path, session_id: str):
        self._session_path = Path(base_dir) / session_id
        self._session_path.mkdir(parents=True, exist_ok=True)
        # One staged write per path; staging the same path twice in a
        # transaction replaces the earlier op (last write wins).
        self._staged_ops: dict[str, StagedOperation] = {}
        self._staged_deletes: set[str] = set()
        self._lock = asyncio.Lock()

    def resolve(self, path: str | Path) -> Path:
        return self._session_path / path

    def _clear_staged_under_lock(self) -> list[Coroutine[Any, Any, None]]:
        """Clear all staged ops and deletes; return the clear callbacks so
        the caller can await them outside the lock. Caller must hold
        ``self._lock``."""
        clear_tasks: list[Coroutine[Any, Any, None]] = [
            op.clear() for op in self._staged_ops.values() if op.clear
        ]
        self._staged_ops.clear()
        self._staged_deletes.clear()
        return clear_tasks

    async def clear_staged(self) -> None:
        async with self._lock:
            clear_tasks = self._clear_staged_under_lock()
        if clear_tasks:
            await asyncio.gather(*clear_tasks)

    async def _atomically_write_bytes(
        self, target_path: Path, content: bytes
    ) -> None:
        """Write ``content`` to ``target_path`` atomically via a temp file."""

        def _write_temp_sync(target_dir: Path) -> str:
            f = tempfile.NamedTemporaryFile(
                mode="wb", dir=target_dir, delete=False
            )
            try:
                f.write(content)
                f.close()
                return f.name
            except BaseException:
                # Don't leak the temp file if the write itself fails (e.g.
                # disk full) before we return the path.
                f.close()
                try:
                    os.unlink(f.name)
                except OSError:
                    pass
                raise

        temp_path_str = await asyncio.to_thread(_write_temp_sync, target_path.parent)
        try:
            await asyncio.to_thread(os.replace, temp_path_str, str(target_path))
        except BaseException:
            await asyncio.to_thread(
                lambda p=temp_path_str: Path(p).unlink(missing_ok=True)
            )
            raise

    async def commit_staged(self) -> None:
        async with self._lock:
            # Handle deletes first.
            for rel_path_str in self._staged_deletes:
                target_path = self.resolve(rel_path_str)
                await asyncio.to_thread(
                    lambda p=target_path: p.unlink(missing_ok=True)
                )

            # Resolve each staged write and atomically replace its target.
            for rel_path_str, op in self._staged_ops.items():
                content = await op.write()
                target_path = self.resolve(rel_path_str)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                await self._atomically_write_bytes(target_path, content)

            # Clear under the same lock so a concurrent stage_* between disk
            # writes and clear cannot have its work silently discarded. Clear
            # callbacks themselves are awaited outside the lock so a callback
            # that re-enters FileClient cannot deadlock.
            clear_tasks = self._clear_staged_under_lock()
        if clear_tasks:
            await asyncio.gather(*clear_tasks)

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
        the bytes at commit time (useful when the final content depends on
        state that accumulates during the transaction). Callers needing
        "append" semantics can implement them in a callback that reads the
        existing file content and concatenates the new data.

        Staging the same path twice in one transaction replaces the earlier
        op; the displaced op's ``clear_callback`` is not invoked.
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
