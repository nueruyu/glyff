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

    async def clear_staged(self) -> None:
        clear_tasks = []
        for ops in self._staged_ops.values():
            for op in ops:
                if op.clear:
                    clear_tasks.append(op.clear())
        if clear_tasks:
            await asyncio.gather(*clear_tasks)

        self._staged_ops.clear()
        self._staged_deletes.clear()

    async def _resolve_staged_ops_to_bytes(
        self, ops: list[StagedOperation]
    ) -> list[bytes]:
        """Resolve a sequence of staged operations into a final list of byte
        chunks. A WRITE discards the chunks that came before it; APPENDs
        accumulate. Collapsing here lets us write the final bytes with a
        single open() per file."""
        final_chunks: list[bytes] = []
        for op in ops:
            content = await op.write()
            if op.mode == OpMode.WRITE:
                final_chunks = [content]
            else:  # OpMode.APPEND
                final_chunks.append(content)
        return final_chunks

    async def _atomically_write_bytes(
        self, target_path: Path, chunks: list[bytes]
    ) -> None:
        """Write byte chunks to ``target_path`` atomically via a temp file."""
        if not chunks:
            return

        def _write_temp_sync(target_dir: Path) -> str:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target_dir, delete=False
            ) as f:
                for buf in chunks:
                    f.write(buf)
                return f.name

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
                final_chunks = await self._resolve_staged_ops_to_bytes(ops)
                if not final_chunks:
                    continue
                target_path = self.resolve(rel_path_str)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                await self._atomically_write_bytes(target_path, final_chunks)

        await self.clear_staged()

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
        self._staged_deletes.add(rel_str)
        if rel_str in self._staged_ops:
            # Run clear callbacks for the cancelled operations.
            ops_to_clear = self._staged_ops.pop(rel_str)
            clear_tasks = [op.clear() for op in ops_to_clear if op.clear]
            if clear_tasks:
                await asyncio.gather(*clear_tasks)
