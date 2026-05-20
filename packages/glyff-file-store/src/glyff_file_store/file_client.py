from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, NamedTuple

WriteCallback = Callable[[], Awaitable[bytes]]
ClearCallback = Callable[[], Coroutine[Any, Any, None]]


class StagedWrite(NamedTuple):
    write: WriteCallback
    clear: ClearCallback | None


class FileClient:
    """A low-level file-based data store with transactional capabilities."""

    def __init__(self, base_dir: str | Path, session_id: str):
        self._session_path = Path(base_dir) / session_id
        self._session_path.mkdir(parents=True, exist_ok=True)
        self._staged_writes: dict[str, StagedWrite] = {}
        self._staged_deletes: set[str] = set()
        self._lock = asyncio.Lock()

    def resolve(self, path: str | Path) -> Path:
        return self._session_path / path

    async def clear_staged(self) -> None:
        clear_tasks = [
            staged.clear() for staged in self._staged_writes.values() if staged.clear
        ]
        if clear_tasks:
            await asyncio.gather(*clear_tasks)

        self._staged_writes.clear()
        self._staged_deletes.clear()

    async def commit_staged(self) -> None:
        def _write_temp_file(content: bytes, target_dir: Path) -> str:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target_dir, delete=False
            ) as f:
                f.write(content)
                return f.name

        async with self._lock:
            temp_paths: dict[str, str] = {}
            try:
                for rel_path_str, staged_write in self._staged_writes.items():
                    target_path = self.resolve(rel_path_str)
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    content = await staged_write.write()
                    temp_paths[rel_path_str] = await asyncio.to_thread(
                        _write_temp_file, content, target_path.parent
                    )

                for rel_path_str, temp_path in temp_paths.items():
                    target_path = self.resolve(rel_path_str)
                    await asyncio.to_thread(os.replace, temp_path, str(target_path))

                for rel_path_str in self._staged_deletes:
                    target_path = self.resolve(rel_path_str)
                    await asyncio.to_thread(
                        lambda p=target_path: p.unlink(missing_ok=True)
                    )
            finally:
                unlink_tasks = [
                    asyncio.to_thread(os.unlink, temp_path)
                    for temp_path in temp_paths.values()
                    if os.path.exists(temp_path)
                ]
                if unlink_tasks:
                    await asyncio.gather(*unlink_tasks)

        await self.clear_staged()

    async def read(self, path: str | Path) -> bytes | None:
        try:
            return await asyncio.to_thread(self.resolve(path).read_bytes)
        except FileNotFoundError:
            return None

    async def stage_write(
        self,
        path: str | Path,
        write_callback: WriteCallback,
        clear_callback: ClearCallback | None = None,
    ) -> None:
        rel_str = str(path)
        self._staged_writes[rel_str] = StagedWrite(
            write=write_callback, clear=clear_callback
        )
        if rel_str in self._staged_deletes:
            self._staged_deletes.remove(rel_str)

    async def stage_delete(self, path: str | Path) -> None:
        rel_str = str(path)
        self._staged_deletes.add(rel_str)
        if rel_str in self._staged_writes:
            del self._staged_writes[rel_str]
