from __future__ import annotations

import asyncio
import os
import tempfile
from collections import defaultdict
from enum import Enum, auto
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, NamedTuple, Union

WriteCallback = Callable[[], Awaitable[bytes]]
ClearCallback = Callable[[], Coroutine[Any, Any, None]]
Content = Union[bytes, WriteCallback]


class OpMode(Enum):
    WRITE = auto()
    APPEND = auto()


class StagedOperation(NamedTuple):
    mode: OpMode
    write: WriteCallback
    clear: ClearCallback | None


class FileClient:
    """A low-level file-based data store with transactional capabilities."""

    def __init__(self, base_dir: str | Path, session_id: str):
        self._session_path = Path(base_dir) / session_id
        self._session_path.mkdir(parents=True, exist_ok=True)
        self._staged_ops: dict[str, list[StagedOperation]] = defaultdict(list)
        self._staged_deletes: set[str] = set()
        self._lock = asyncio.Lock()

    def resolve(self, path: str | Path) -> Path:
        return self._session_path / path

    async def _clear_staged_under_lock(self) -> None:
        """Clear all staged ops and deletes. Caller must hold ``self._lock``."""
        clear_tasks = []
        for ops in self._staged_ops.values():
            for op in ops:
                if op.clear:
                    clear_tasks.append(op.clear())
        if clear_tasks:
            await asyncio.gather(*clear_tasks)

        self._staged_ops.clear()
        self._staged_deletes.clear()

    async def clear_staged(self) -> None:
        async with self._lock:
            await self._clear_staged_under_lock()

    async def _resolve_staged_ops_to_bytes(
        self, rel_path_str: str, ops: list[StagedOperation]
    ) -> list[bytes]:
        """Resolve a sequence of staged operations into a final list of byte
        chunks. A WRITE discards everything before it (including existing
        on-disk content); APPENDs accumulate. If the sequence begins with
        APPENDs (no prior WRITE), the existing on-disk content is read once
        and prepended so that the atomic temp-file write preserves it."""
        final_chunks: list[bytes] = []
        existing_loaded = False
        for op in ops:
            content = await op.write()
            if op.mode == OpMode.WRITE:
                final_chunks = [content]
                existing_loaded = True
            else:  # OpMode.APPEND
                if not existing_loaded:
                    existing = await self.read(rel_path_str) or b""
                    if existing:
                        final_chunks.append(existing)
                    existing_loaded = True
                final_chunks.append(content)
        return final_chunks

    async def _atomically_write_bytes(
        self, target_path: Path, chunks: list[bytes]
    ) -> None:
        """Write byte chunks to ``target_path`` atomically via a temp file."""
        if not chunks:
            return

        def _write_temp_sync(target_dir: Path) -> str:
            f = tempfile.NamedTemporaryFile(
                mode="wb", dir=target_dir, delete=False
            )
            try:
                for buf in chunks:
                    f.write(buf)
                f.close()
                return f.name
            except BaseException:
                # Make sure we don't leak the temp file if the write itself
                # fails (e.g. disk full) before we return the path.
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

            # Process writes and appends in order.
            for rel_path_str, ops in self._staged_ops.items():
                if not ops:
                    continue
                final_chunks = await self._resolve_staged_ops_to_bytes(
                    rel_path_str, ops
                )
                if not final_chunks:
                    continue
                target_path = self.resolve(rel_path_str)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                await self._atomically_write_bytes(target_path, final_chunks)

            # Clear under the same lock so a concurrent stage_* between
            # disk writes and clear cannot have its work silently discarded.
            await self._clear_staged_under_lock()

    async def read(self, path: str | Path) -> bytes | None:
        try:
            return await asyncio.to_thread(self.resolve(path).read_bytes)
        except FileNotFoundError:
            return None

    async def _stage_op(
        self,
        mode: OpMode,
        path: str | Path,
        content: Content,
        clear_callback: ClearCallback | None = None,
    ) -> None:
        async def bytes_writer() -> bytes:
            return content if isinstance(content, bytes) else b""

        callback = content if callable(content) else bytes_writer
        op = StagedOperation(mode=mode, write=callback, clear=clear_callback)

        rel_str = str(path)
        async with self._lock:
            self._staged_ops[rel_str].append(op)
            if rel_str in self._staged_deletes:
                self._staged_deletes.remove(rel_str)

    async def stage_write(
        self,
        path: str | Path,
        content: Content,
        clear_callback: ClearCallback | None = None,
    ) -> None:
        await self._stage_op(OpMode.WRITE, path, content, clear_callback)

    async def stage_append(
        self,
        path: str | Path,
        content: Content,
        clear_callback: ClearCallback | None = None,
    ) -> None:
        await self._stage_op(OpMode.APPEND, path, content, clear_callback)

    async def stage_delete(self, path: str | Path) -> None:
        rel_str = str(path)
        async with self._lock:
            self._staged_deletes.add(rel_str)
            ops_to_clear = self._staged_ops.pop(rel_str, [])
        # Run clear callbacks outside the lock so a callback that re-enters
        # FileClient (e.g., via stage_*) cannot deadlock.
        clear_tasks = [op.clear() for op in ops_to_clear if op.clear]
        if clear_tasks:
            await asyncio.gather(*clear_tasks)
