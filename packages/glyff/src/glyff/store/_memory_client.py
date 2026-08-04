from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MemoryUpdate = Callable[[Any | None], Any | None]

# Records are keyed by their parts rather than a joined string, so a session id
# can hold any character without colliding with the separator.
MemoryKey = tuple[str, ...]


@dataclass(frozen=True)
class _Write:
    value: Any


@dataclass(frozen=True)
class _Delete:
    pass


@dataclass(frozen=True)
class _Update:
    fn: MemoryUpdate


_StagedOp = _Write | _Delete | _Update


class _StagingBuffer:
    __slots__ = ("ops",)

    def __init__(self) -> None:
        self.ops: dict[MemoryKey, list[_StagedOp]] = {}

    def clear(self) -> None:
        self.ops.clear()


class MemoryClient:
    """A low-level in-memory data store with per-transaction staging."""

    def __init__(self):
        self._data: dict[MemoryKey, Any] = {}
        self._current: contextvars.ContextVar[_StagingBuffer | None] = (
            contextvars.ContextVar("memory_client_staging", default=None)
        )
        self._lock = asyncio.Lock()

    @property
    def data(self) -> dict[MemoryKey, Any]:
        return self._data

    def _apply_ops(self, initial: Any | None, ops: list[_StagedOp]) -> Any | None:
        current = initial
        for op in ops:
            if isinstance(op, _Write):
                current = op.value
            elif isinstance(op, _Delete):
                current = None
            elif isinstance(op, _Update):
                current = op.fn(current)
            else:
                raise TypeError(f"Unknown op: {op!r}")
        return current

    def _require_staging(self) -> _StagingBuffer:
        staging = self._current.get()
        if staging is None:
            raise RuntimeError("MemoryClient write attempted outside a transaction.")
        return staging

    def begin_staging(self) -> tuple[contextvars.Token, _StagingBuffer]:
        staging = _StagingBuffer()
        token = self._current.set(staging)
        return token, staging

    def end_staging(self, token: contextvars.Token) -> None:
        self._current.reset(token)

    def _require_current_staging(self, expected: _StagingBuffer) -> None:
        if self._current.get() is not expected:
            raise RuntimeError("Transaction closed out of order.")

    def all_keys(self) -> set[MemoryKey]:
        buffer = self._current.get()
        if buffer is None:
            return set(self._data.keys())

        keys = set(self._data.keys())
        for key, ops in buffer.ops.items():
            current = self._data.get(key)
            result = self._apply_ops(current, ops)
            if result is None:
                keys.discard(key)
            else:
                keys.add(key)
        return keys

    def clear_staged(self) -> None:
        self._require_staging().clear()

    async def commit_staged(self) -> None:
        buffer = self._require_staging()
        async with self._lock:
            for key, ops in buffer.ops.items():
                result = self._apply_ops(self._data.get(key), ops)
                if result is None:
                    self._data.pop(key, None)
                else:
                    self._data[key] = result
        buffer.clear()

    async def read(self, key: MemoryKey, *, staged: bool = True) -> Any | None:
        async with self._lock:
            buffer = self._current.get()
            if staged and buffer is not None and key in buffer.ops:
                return self._apply_ops(self._data.get(key), buffer.ops[key])
            return self._data.get(key)

    def stage_write(self, key: MemoryKey, value: Any) -> None:
        buffer = self._require_staging()
        buffer.ops.setdefault(key, []).append(_Write(value))

    def stage_delete(self, key: MemoryKey) -> None:
        buffer = self._require_staging()
        buffer.ops.setdefault(key, []).append(_Delete())

    def stage_update(self, key: MemoryKey, fn: MemoryUpdate) -> None:
        buffer = self._require_staging()
        buffer.ops.setdefault(key, []).append(_Update(fn))
