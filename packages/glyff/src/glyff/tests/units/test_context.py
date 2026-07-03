import pytest

from collections.abc import Iterable

from glyff import Execution, ExecutionId, SessionStore, Transaction
from glyff._context import TransactionScope, get_context
from glyff.exceptions import ContextNotSetError


class FakeTransaction(Transaction):
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeStore(SessionStore):
    def __init__(self) -> None:
        self.transaction = FakeTransaction()
        self.begins = 0

    async def begin_transaction(self) -> FakeTransaction:
        self.begins += 1
        return self.transaction

    async def get(self, execution_id: ExecutionId) -> Execution | None:
        raise NotImplementedError

    async def save(self, execution: Execution) -> None:
        raise NotImplementedError

    async def descendants_of(self, execution_id: ExecutionId) -> list[ExecutionId]:
        raise NotImplementedError

    async def delete_many(self, execution_ids: Iterable[ExecutionId]) -> None:
        raise NotImplementedError


def test_get_context_raises_custom_error_when_unset():
    with pytest.raises(ContextNotSetError, match="Workflow context is not set"):
        get_context()


async def test_transaction_scope_commits_on_normal_exit():
    store = FakeStore()

    async with TransactionScope(store):
        pass

    assert store.begins == 1
    assert store.transaction.commits == 1
    assert store.transaction.rollbacks == 0


async def test_transaction_scope_rolls_back_on_exception():
    store = FakeStore()

    with pytest.raises(ValueError, match="boom"):
        async with TransactionScope(store):
            raise ValueError("boom")

    assert store.transaction.commits == 0
    assert store.transaction.rollbacks == 1


async def test_explicit_commit_prevents_exit_rollback():
    store = FakeStore()

    with pytest.raises(ValueError, match="boom"):
        async with TransactionScope(store) as scope:
            await scope.commit()
            raise ValueError("boom")

    assert store.transaction.commits == 1
    assert store.transaction.rollbacks == 0


async def test_transaction_scope_cannot_close_twice():
    store = FakeStore()

    async with TransactionScope(store) as scope:
        await scope.commit()
        with pytest.raises(RuntimeError, match="already closed"):
            await scope.rollback()

    assert store.transaction.commits == 1
    assert store.transaction.rollbacks == 0
