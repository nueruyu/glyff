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
        self._staged_overwrites: dict[str, StagedWrite] = {}
        self._staged_appends: dict[str, list[StagedWrite]] = {}
        self._staged_deletes: set[str] = set()
        self._lock = asyncio.Lock()

    def resolve(self, path: str | Path) -> Path:
        return self._session_path / path

    async def clear_staged(self) -> None:
        clear_tasks = []
        for staged in self._staged_overwrites.values():
            if staged.clear:
                clear_tasks.append(staged.clear())
        for staged_list in self._staged_appends.values():
            for staged in staged_list:
                if staged.clear:
                    clear_tasks.append(staged.clear())

        if clear_tasks:
            await asyncio.gather(*clear_tasks)

        self._staged_overwrites.clear()
        self._staged_appends.clear()
        self._staged_deletes.clear()

    async def commit_staged(self, final_commit_path: str | Path | None = None) -> None:
        """
        Commits all staged operations using a 2-phase approach.

        Phase 1: applies appends and all overwrites except the final_commit_path.
        Phase 2: atomically writes the final_commit_path last, acting as the
        durability marker (2PC commit point).
        """
        final_commit_rel_path = str(final_commit_path) if final_commit_path else None

        def _write_temp_file(content: bytes, target_dir: Path) -> str:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target_dir, delete=False
            ) as f:
                f.write(content)
                return f.name

        def _append_to_file(content: bytes, target_path: Path) -> None:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "ab") as f:
                f.write(content)

        async with self._lock:
            temp_paths: dict[str, str] = {}
            final_overwrite_op: tuple[str, StagedWrite] | None = None

            try:
                # Phase 1a: Prepare overwrites (excluding the final commit path)
                for rel_path, op in self._staged_overwrites.items():
                    if rel_path == final_commit_rel_path:
                        final_overwrite_op = (rel_path, op)
                        continue
                    target_path = self.resolve(rel_path)
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    content = await op.write()
                    temp_paths[rel_path] = await asyncio.to_thread(
                        _write_temp_file, content, target_path.parent
                    )

                # Phase 1b: Atomically replace overwrite targets
                for rel_path, temp_path in list(temp_paths.items()):
                    if rel_path == final_commit_rel_path:
                        continue
                    target_path = self.resolve(rel_path)
                    await asyncio.to_thread(os.replace, temp_path, str(target_path))
                    del temp_paths[rel_path]

                # Phase 1c: Apply appends
                for rel_path, op_list in self._staged_appends.items():
                    target_path = self.resolve(rel_path)
                    for op in op_list:
                        content = await op.write()
                        await asyncio.to_thread(_append_to_file, content, target_path)

                # Phase 1d: Apply deletes (skip final commit path)
                for rel_path in self._staged_deletes:
                    if rel_path == final_commit_rel_path:
                        continue
                    target_path = self.resolve(rel_path)
                    await asyncio.to_thread(
                        lambda p=target_path: p.unlink(missing_ok=True)
                    )

                # Phase 2: Atomically write the final commit path
                if final_overwrite_op:
                    rel_path, op = final_overwrite_op
                    target_path = self.resolve(rel_path)
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    content = await op.write()
                    temp_final = await asyncio.to_thread(
                        _write_temp_file, content, target_path.parent
                    )
                    temp_paths[rel_path] = temp_final
                    await asyncio.to_thread(os.replace, temp_final, str(target_path))
                    del temp_paths[rel_path]

            finally:
                unlink_tasks = [
                    asyncio.to_thread(os.unlink, tp)
                    for tp in temp_paths.values()
                    if os.path.exists(tp)
                ]
                if unlink_tasks:
                    await asyncio.gather(*unlink_tasks)

        await self.clear_staged()

    async def read(self, path: str | Path) -> bytes | None:
        try:
            return await asyncio.to_thread(self.resolve(path).read_bytes)
        except FileNotFoundError:
            return None

    async def stage_overwrite(
        self,
        path: str | Path,
        write_callback: WriteCallback,
        clear_callback: ClearCallback | None = None,
    ) -> None:
        rel_str = str(path)
        self._staged_overwrites[rel_str] = StagedWrite(
            write=write_callback, clear=clear_callback
        )
        if rel_str in self._staged_deletes:
            self._staged_deletes.remove(rel_str)

    async def stage_append(
        self,
        path: str | Path,
        write_callback: WriteCallback,
        clear_callback: ClearCallback | None = None,
    ) -> None:
        rel_str = str(path)
        if rel_str not in self._staged_appends:
            self._staged_appends[rel_str] = []
        self._staged_appends[rel_str].append(
            StagedWrite(write=write_callback, clear=clear_callback)
        )

    async def stage_delete(self, path: str | Path) -> None:
        rel_str = str(path)
        self._staged_deletes.add(rel_str)
        if rel_str in self._staged_overwrites:
            del self._staged_overwrites[rel_str]
        if rel_str in self._staged_appends:
            del self._staged_appends[rel_str]
