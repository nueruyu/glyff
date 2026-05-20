from __future__ import annotations

from typing import Any, NamedTuple

from glyff import ExecutionId
from glyff.interfaces import Execution, Serializer, SessionStore, Transaction
from glyff.models import ExecutionRecord
from glyff.stores import MemorySessionStore
from glyff.stores.memory_client import MemoryClient


class Call(NamedTuple):
    name: str
    args: tuple
    kwargs: dict


class StubExecution(Execution):
    def __init__(self, store: StubSessionStore, eid: ExecutionId):
        self._store = store
        self._id = eid

    async def complete(self, value: Any, return_type: type) -> None:
        self._store._record("complete", self._id, value, return_type)
        await self._store.execution_impl.complete(value, return_type)

    async def fail(self, error: str) -> None:
        self._store._record("fail", self._id, error)
        await self._store.execution_impl.fail(error)


class StubTransaction(Transaction):
    def __init__(self, store: StubSessionStore):
        self._store = store

    async def commit(self) -> None:
        self._store._record("commit")
        await self._store.transaction_impl.commit()

    async def rollback(self) -> None:
        self._store._record("rollback")
        await self._store.transaction_impl.rollback()


class StubSessionStore(SessionStore):
    """A spy over MemorySessionStore that records method calls for test assertions."""

    def __init__(self, client: MemoryClient, serializer: Serializer, **_):
        self._mem_store = MemorySessionStore(client=client, serializer=serializer)
        self.calls: list[Call] = []
        self.transaction_impl: Transaction
        self.execution_impl: Execution

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(Call(name, args, kwargs))

    def get_calls(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name]

    async def begin_transaction(self) -> Transaction:
        self._record("begin_transaction")
        self.transaction_impl = await self._mem_store.begin_transaction()
        return StubTransaction(self)

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        self._record("start_execution", execution_id)
        self.execution_impl = await self._mem_store.start_execution(execution_id)
        return StubExecution(self, execution_id)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        self._record("get_execution_record", execution_id, return_type)
        return await self._mem_store.get_execution_record(execution_id, return_type)
