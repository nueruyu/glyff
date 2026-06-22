from __future__ import annotations

import asyncio
from typing import Any


class MemoryClient:
    """A low-level in-memory data store.

    Writes are applied immediately. The transaction-shaped methods remain for
    compatibility with older store code, but they no longer gate durability.
    """

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def all_keys(self) -> set[str]:
        return set(self._data.keys())

    def clear_staged(self) -> None:
        return None

    async def commit_staged(self) -> None:
        return None

    async def read(self, key: str, *, staged: bool = True) -> Any | None:
        """Read the value for ``key``.

        The ``staged`` argument is kept for API compatibility and is ignored;
        there is no staged view now that writes are per-event.
        """
        async with self._lock:
            return self._data.get(key)

    def stage_write(self, key: str, value: Any) -> None:
        self._data[key] = value

    def stage_delete(self, key: str) -> None:
        self._data.pop(key, None)
