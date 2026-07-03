import pytest

from glyff import Transaction, TransactionProvider
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


class FakeTransactions(TransactionProvider):
    def __init__(self) -> None:
        self.transaction = FakeTransaction()
        self.begins = 0

    async def begin_transaction(self) -> FakeTransaction:
        self.begins += 1
        return self.transaction


def test_get_context_raises_custom_error_when_unset():
    with pytest.raises(ContextNotSetError, match="Workflow context is not set"):
        get_context()


async def test_transaction_scope_commits_on_normal_exit():
    store = FakeTransactions()

    async with TransactionScope(store):
        pass

    assert store.begins == 1
    assert store.transaction.commits == 1
    assert store.transaction.rollbacks == 0


async def test_transaction_scope_rolls_back_on_exception():
    store = FakeTransactions()

    with pytest.raises(ValueError, match="boom"):
        async with TransactionScope(store):
            raise ValueError("boom")

    assert store.transaction.commits == 0
    assert store.transaction.rollbacks == 1


async def test_explicit_commit_prevents_exit_rollback():
    store = FakeTransactions()

    with pytest.raises(ValueError, match="boom"):
        async with TransactionScope(store) as scope:
            await scope.commit()
            raise ValueError("boom")

    assert store.transaction.commits == 1
    assert store.transaction.rollbacks == 0


async def test_transaction_scope_cannot_close_twice():
    store = FakeTransactions()

    async with TransactionScope(store) as scope:
        await scope.commit()
        with pytest.raises(RuntimeError, match="already closed"):
            await scope.rollback()

    assert store.transaction.commits == 1
    assert store.transaction.rollbacks == 0
