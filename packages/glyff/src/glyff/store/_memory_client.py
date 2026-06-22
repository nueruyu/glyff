from __future__ import annotations

import asyncio
import contextvars
from typing import Any


class _StagingBuffer:
    """A single transaction's pending writes and deletes."""

    __slots__ = ("writes", "deletes")

    def __init__(self) -> None:
        self.writes: dict[str, Any] = {}
        self.deletes: set[str] = set()

    def clear(self) -> None:
        self.writes.clear()
        self.deletes.clear()


class MemoryClient:
    """A low-level in-memory data store with per-transaction staging.

    Each transaction stages into its own buffer, tracked per asyncio task via a
    ``ContextVar``. Concurrent transactions (e.g. parallel ``asyncio.gather``
    branches, which each run in a copied context) therefore stay isolated: one
    transaction's commit or rollback never touches another's staged writes.

    Outside any transaction, staging falls back to a shared ambient buffer so
    the client can still be used as a standalone staging primitive.
    """

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._ambient = _StagingBuffer()
        # Per-instance so two stores (two sessions) never share a staging view.
        self._current: contextvars.ContextVar[_StagingBuffer | None] = (
            contextvars.ContextVar("memory_client_staging", default=None)
        )
        self._lock = asyncio.Lock()

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def _buffer(self) -> _StagingBuffer:
        """The staging buffer for the current transaction, or the ambient one."""
        return self._current.get() or self._ambient

    # ------------------------------------------------------------------
    # Transaction lifecycle (driven by the store's Transaction object)
    # ------------------------------------------------------------------

    def begin_staging(self) -> contextvars.Token:
        """Start an isolated staging buffer for the current task; returns a
        token that ``end_staging`` uses to restore the previous buffer."""
        return self._current.set(_StagingBuffer())

    def end_staging(self, token: contextvars.Token) -> None:
        try:
            self._current.reset(token)
        except (ValueError, LookupError):
            # Reset from a different context than set; nothing to restore.
            pass

    def all_keys(self) -> set[str]:
        """All keys visible to the current transaction: committed keys plus
        keys staged for writing, minus those staged for deletion."""
        buffer = self._buffer()
        return (self._data.keys() | buffer.writes.keys()) - buffer.deletes

    def clear_staged(self) -> None:
        self._buffer().clear()

    async def commit_staged(self) -> None:
        buffer = self._buffer()
        async with self._lock:
            self._data.update(buffer.writes)
            for key in buffer.deletes:
                self._data.pop(key, None)
        buffer.clear()

    async def read(self, key: str, *, staged: bool = True) -> Any | None:
        """Read the value for ``key``.

        With ``staged=True`` (the default) the read is transaction-aware: a
        staged write overrides the committed value, a staged delete reads as
        ``None``, and otherwise the committed value is returned (mirroring the
        staged view exposed by ``all_keys()``). With ``staged=False`` only the
        committed value is returned, ignoring all staged state."""
        async with self._lock:
            if staged:
                buffer = self._buffer()
                if key in buffer.deletes:
                    return None
                if key in buffer.writes:
                    return buffer.writes[key]
            return self._data.get(key)

    def stage_write(self, key: str, value: Any) -> None:
        buffer = self._buffer()
        buffer.writes[key] = value
        buffer.deletes.discard(key)

    def stage_delete(self, key: str) -> None:
        buffer = self._buffer()
        buffer.deletes.add(key)
        buffer.writes.pop(key, None)
