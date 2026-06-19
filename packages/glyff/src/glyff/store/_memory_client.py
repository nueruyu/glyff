from __future__ import annotations

import asyncio
from typing import Any


class MemoryClient:
    """A low-level in-memory data store with transactional capabilities."""

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._staged_writes: dict[str, Any] = {}
        self._staged_deletes: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def all_keys(self) -> set[str]:
        """All keys visible to the current transaction: committed keys plus
        keys staged for writing, minus those staged for deletion."""
        return (self._data.keys() | self._staged_writes.keys()) - self._staged_deletes

    def clear_staged(self) -> None:
        self._staged_writes.clear()
        self._staged_deletes.clear()

    async def commit_staged(self) -> None:
        async with self._lock:
            self._data.update(self._staged_writes)
            for key in self._staged_deletes:
                self._data.pop(key, None)
        self.clear_staged()

    async def read(self, key: str, *, staged: bool = True) -> Any | None:
        """Read the value for ``key``.

        With ``staged=True`` (the default) the read is transaction-aware: a
        staged write overrides the committed value, a staged delete reads as
        ``None``, and otherwise the committed value is returned (mirroring the
        staged view exposed by ``all_keys()``). With ``staged=False`` only the
        committed value is returned, ignoring all staged state."""
        async with self._lock:
            if staged:
                if key in self._staged_deletes:
                    return None
                if key in self._staged_writes:
                    return self._staged_writes[key]
            return self._data.get(key)

    def stage_write(self, key: str, value: Any) -> None:
        self._staged_writes[key] = value
        self._staged_deletes.discard(key)

    def stage_delete(self, key: str) -> None:
        self._staged_deletes.add(key)
        self._staged_writes.pop(key, None)
