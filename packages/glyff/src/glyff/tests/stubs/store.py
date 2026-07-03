from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, NamedTuple

from glyff import Execution, ExecutionId, Serializer, SessionStore, Transaction
from glyff.store import MemorySessionStore
from glyff.store._memory_client import MemoryClient


class Call(NamedTuple):
    name: str
    args: tuple
    kwargs: dict


Recorder = Callable[..., None]


class StubTransaction(Transaction):
    def __init__(self, record: Recorder, impl: Transaction):
        self._record = record
        self._impl = impl

    async def commit(self) -> None:
        self._record("commit")
        await self._impl.commit()

    async def rollback(self) -> None:
        self._record("rollback")
        await self._impl.rollback()


class StubSessionStore(SessionStore):
    """A spy over MemoryExecutionRepository."""

    def __init__(self, client: MemoryClient, serializer: Serializer, **_):
        self._mem_store = MemorySessionStore(client=client, serializer=serializer)
        self.serializer = serializer
        self.calls: list[Call] = []
        self.transaction_impl: Transaction

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(Call(name, args, kwargs))

    def get_calls(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name]

    async def begin_transaction(self) -> Transaction:
        self._record("begin_transaction")
        self.transaction_impl = await self._mem_store.begin_transaction()
        return StubTransaction(self._record, self.transaction_impl)

    async def get(self, execution_id: ExecutionId) -> Execution | None:
        self._record("get", execution_id)
        return await self._mem_store.get(execution_id)

    async def save(self, execution: Execution) -> None:
        self._record("save", execution)
        await self._mem_store.save(execution)

    async def descendants_of(self, execution_id: ExecutionId) -> list[ExecutionId]:
        self._record("descendants_of", execution_id)
        return await self._mem_store.descendants_of(execution_id)

    async def delete_many(self, execution_ids: Iterable[ExecutionId]) -> None:
        execution_ids = list(execution_ids)
        self._record("delete_many", execution_ids)
        await self._mem_store.delete_many(execution_ids)
