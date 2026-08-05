from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any, NamedTuple

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRepository,
    ExecutionStatus,
    SessionId,
    Transaction,
    TransactionProvider,
)
from glyff.store import (
    MemoryBackend,
    MemoryExecutionRepository,
    MemoryTransactionProvider,
)
from glyff.store._memory_client import MemoryClient
from glyff.store.execution_stage import ExecutionStage


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


class StubExecutionRepository(ExecutionRepository):
    def __init__(self, record: Recorder, impl: ExecutionRepository):
        self._record = record
        self._impl = impl

    async def get(
        self, session_id: SessionId, execution_id: ExecutionId
    ) -> Execution | None:
        self._record("get", execution_id)
        return await self._impl.get(session_id, execution_id)

    async def save(self, session_id: SessionId, execution: Execution) -> None:
        self._record("save", execution)
        await self._impl.save(session_id, execution)

    def executions(
        self,
        session_id: SessionId,
        *,
        status: ExecutionStatus | None = None,
        under: ExecutionId | None = None,
    ) -> AsyncIterator[Execution]:
        self._record("executions", status=status, under=under)
        return self._impl.executions(session_id, status=status, under=under)

    async def delete_many(
        self, session_id: SessionId, execution_ids: Iterable[ExecutionId]
    ) -> None:
        execution_ids = list(execution_ids)
        self._record("delete_many", execution_ids)
        await self._impl.delete_many(session_id, execution_ids)


class StubTransactionProvider(TransactionProvider):
    def __init__(self, record: Recorder, impl: TransactionProvider):
        self._record = record
        self._impl = impl
        self.transaction_impl: Transaction | None = None

    async def begin_transaction(self) -> Transaction:
        self._record("begin_transaction")
        self.transaction_impl = await self._impl.begin_transaction()
        return StubTransaction(self._record, self.transaction_impl)


class StubBackend:
    def __init__(self, client: MemoryClient):
        self.calls: list[Call] = []
        self._client = client
        self._backend = MemoryBackend()
        self.stage = ExecutionStage()
        self.repository = StubExecutionRepository(
            self._record, MemoryExecutionRepository(client, self.stage)
        )
        self.transaction_provider = StubTransactionProvider(
            self._record, MemoryTransactionProvider(client, self.stage)
        )

    async def claim_session(self, session_id: SessionId, app_version: str) -> str:
        self._record("claim_session", session_id, app_version)
        return await self._backend.claim_session(session_id, app_version)

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(Call(name, args, kwargs))

    def get_calls(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name]
