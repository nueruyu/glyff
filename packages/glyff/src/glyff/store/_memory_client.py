from __future__ import annotations

import asyncio
import contextvars
from typing import Any


class _StagingBuffer:
    __slots__ = ("writes", "deletes")

    def __init__(self) -> None:
        self.writes: dict[str, Any] = {}
        self.deletes: set[str] = set()

    def clear(self) -> None:
        self.writes.clear()
        self.deletes.clear()


class MemoryClient:
    """A low-level in-memory data store with per-transaction staging."""

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._current: contextvars.ContextVar[_StagingBuffer | None] = (
            contextvars.ContextVar("memory_client_staging", default=None)
        )
        self._lock = asyncio.Lock()

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def _require_staging(self) -> _StagingBuffer:
        staging = self._current.get()
        if staging is None:
            raise RuntimeError("MemoryClient write attempted outside a transaction.")
        return staging

    def begin_staging(self) -> contextvars.Token:
        return self._current.set(_StagingBuffer())

    def end_staging(self, token: contextvars.Token) -> None:
        try:
            self._current.reset(token)
        except (ValueError, LookupError):
            pass

    def all_keys(self) -> set[str]:
        buffer = self._current.get()
        if buffer is None:
            return set(self._data.keys())
        return (self._data.keys() | buffer.writes.keys()) - buffer.deletes

    def clear_staged(self) -> None:
        self._require_staging().clear()

    async def commit_staged(self) -> None:
        buffer = self._require_staging()
        async with self._lock:
            self._data.update(buffer.writes)
            for key in buffer.deletes:
                self._data.pop(key, None)
        buffer.clear()

    async def read(self, key: str, *, staged: bool = True) -> Any | None:
        async with self._lock:
            buffer = self._current.get()
            if staged and buffer is not None:
                if key in buffer.deletes:
                    return None
                if key in buffer.writes:
                    return buffer.writes[key]
            return self._data.get(key)

    def stage_write(self, key: str, value: Any) -> None:
        buffer = self._require_staging()
        buffer.writes[key] = value
        buffer.deletes.discard(key)

    def stage_delete(self, key: str) -> None:
        buffer = self._require_staging()
        buffer.deletes.add(key)
        buffer.writes.pop(key, None)
