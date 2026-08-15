from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any, NamedTuple, Protocol

from glyff import (
    Backend,
    DomainId,
    DomainVersion,
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
from glyff.store.staging import ExecutionStaging


class Call(NamedTuple):
    name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class Recorder(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> None: ...


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


class StubBackend(Backend):
    def __init__(self, client: MemoryClient):
        self.calls: list[Call] = []
        self._client = client
        self._backend = MemoryBackend()
        self.staging = ExecutionStaging()
        self._repository = StubExecutionRepository(
            self.record, MemoryExecutionRepository(client, self.staging)
        )
        self._transaction_provider = StubTransactionProvider(
            self.record, MemoryTransactionProvider(client, self.staging)
        )

    @property
    def repository(self) -> StubExecutionRepository:
        return self._repository

    @property
    def transaction_provider(self) -> StubTransactionProvider:
        return self._transaction_provider

    def replace_repository(self, repository: StubExecutionRepository) -> None:
        """Swaps in a repository that fails where the real one would not."""
        self._repository = repository

    @property
    def client(self) -> MemoryClient:
        return self._client

    async def claim_domain(
        self, session_id: SessionId, domain_id: DomainId, version: DomainVersion
    ) -> DomainVersion:
        self.record("claim_domain", session_id, domain_id, version)
        return await self._backend.claim_domain(session_id, domain_id, version)

    def record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(Call(name, args, kwargs))

    def get_calls(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name]
