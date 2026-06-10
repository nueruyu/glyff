from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, NamedTuple

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRecord,
    Serializer,
    SessionStore,
    Transaction,
)
from glyff.store import MemorySessionStore
from glyff.store._memory_client import MemoryClient


class Call(NamedTuple):
    name: str
    args: tuple
    kwargs: dict


Recorder = Callable[..., None]


class StubExecution(Execution):
    def __init__(self, eid: ExecutionId, record: Recorder, impl: Execution):
        self._id = eid
        self._record = record
        self._impl = impl

    async def complete(self, value: Any, return_type: type) -> None:
        self._record("complete", self._id, value, return_type)
        await self._impl.complete(value, return_type)

    async def fail(self, error: str) -> None:
        self._record("fail", self._id, error)
        await self._impl.fail(error)


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
        return StubTransaction(self._record, self.transaction_impl)

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        self._record("start_execution", execution_id)
        self.execution_impl = await self._mem_store.start_execution(execution_id)
        return StubExecution(execution_id, self._record, self.execution_impl)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        self._record("get_execution_record", execution_id, return_type)
        return await self._mem_store.get_execution_record(execution_id, return_type)

    async def get_descendants(self, execution_id: ExecutionId) -> list[ExecutionId]:
        self._record("get_descendants", execution_id)
        return await self._mem_store.get_descendants(execution_id)

    async def delete_executions(self, execution_ids: Iterable[ExecutionId]) -> None:
        execution_ids = list(execution_ids)
        self._record("delete_executions", execution_ids)
        await self._mem_store.delete_executions(execution_ids)
