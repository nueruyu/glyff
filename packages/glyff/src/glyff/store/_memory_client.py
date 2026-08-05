from __future__ import annotations

import asyncio

from .._models import ExecutionId, SessionId
from .staging import (
    DeleteExecution,
    ExecutionKey,
    ExecutionMutation,
    ExecutionSnapshot,
)


class MemoryClient:
    """The committed half of the in-memory store: execution snapshots by key."""

    def __init__(self) -> None:
        self._data: dict[ExecutionKey, ExecutionSnapshot] = {}
        self._lock = asyncio.Lock()

    @property
    def data(self) -> dict[ExecutionKey, ExecutionSnapshot]:
        return self._data

    async def read_committed(self, key: ExecutionKey) -> ExecutionSnapshot | None:
        async with self._lock:
            return self._data.get(key)

    async def committed_for_session(
        self, session_id: SessionId
    ) -> dict[ExecutionId, ExecutionSnapshot]:
        async with self._lock:
            return {
                key.execution_id: snapshot
                for key, snapshot in self._data.items()
                if key.session_id == session_id
            }

    async def commit_mutations(
        self, mutations: dict[ExecutionKey, ExecutionMutation]
    ) -> None:
        async with self._lock:
            for key, mutation in mutations.items():
                if isinstance(mutation, DeleteExecution):
                    self._data.pop(key, None)
                else:
                    self._data[key] = mutation.snapshot
