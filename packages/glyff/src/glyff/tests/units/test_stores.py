import pytest

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRepository,
    ExecutionStatus,
    SerializedValue,
    TransactionProvider,
)
from glyff._context import TransactionScope
from glyff.store import MemoryBackend
from glyff.tests.types import BackendFactory


def _result(value: str) -> SerializedValue:
    return SerializedValue(value.encode())


async def test_get_missing_returns_none(
    backend_factory: BackendFactory, base_execution_id: ExecutionId
):
    backend = backend_factory("test-initial")
    assert await backend.executions.get(base_execution_id) is None


async def test_save_start_stages_execution(
    backend_factory: BackendFactory, base_execution_id: ExecutionId
):
    backend = backend_factory("test-start")
    async with TransactionScope(backend.transactions):
        await backend.executions.save(Execution.start(base_execution_id))
        staged = await backend.executions.get(base_execution_id)
        assert staged is not None
        assert staged.status == ExecutionStatus.STARTED

    state = await backend.executions.get(base_execution_id)
    assert state is not None
    assert state.status == ExecutionStatus.STARTED


async def test_completed_result_persists(
    backend_factory: BackendFactory, base_execution_id: ExecutionId
):
    backend = backend_factory("test-commit-ok")
    execution = Execution.start(base_execution_id)
    execution.complete(_result("42"))

    async with TransactionScope(backend.transactions):
        await backend.executions.save(execution)
        staged = await backend.executions.get(base_execution_id)
        assert staged is not None
        assert staged.status == ExecutionStatus.COMPLETED
        assert staged.result == _result("42")

    state = await backend.executions.get(base_execution_id)
    assert state is not None
    assert state.status == ExecutionStatus.COMPLETED
    assert state.result == _result("42")


async def test_failed_error_persists(
    backend_factory: BackendFactory, base_execution_id: ExecutionId
):
    backend = backend_factory("test-commit-fail")
    execution = Execution.start(base_execution_id)
    execution.fail("something went wrong")
    async with TransactionScope(backend.transactions):
        await backend.executions.save(execution)

    state = await backend.executions.get(base_execution_id)
    assert state is not None
    assert state.status == ExecutionStatus.FAILED
    assert state.error == "something went wrong"


async def test_rollback_discards_save(
    backend_factory: BackendFactory, base_execution_id: ExecutionId
):
    backend = backend_factory("test-rollback")
    scope = TransactionScope(backend.transactions)
    await scope.__aenter__()
    await backend.executions.save(Execution.start(base_execution_id))
    await scope.rollback()
    assert await backend.executions.get(base_execution_id) is None


async def test_save_requires_transaction(
    backend_factory: BackendFactory, base_execution_id: ExecutionId
):
    backend = backend_factory("test-save-without-transaction")

    with pytest.raises(RuntimeError, match="write attempted outside a transaction"):
        await backend.executions.save(Execution.start(base_execution_id))


async def test_delete_many_requires_transaction(
    backend_factory: BackendFactory, base_execution_id: ExecutionId
):
    backend = backend_factory("test-delete-without-transaction")

    with pytest.raises(RuntimeError, match="write attempted outside a transaction"):
        await backend.executions.delete_many([base_execution_id])


async def test_nested_child_commit_is_independent_of_parent_staging(
    backend_factory: BackendFactory, base_execution_id: ExecutionId
):
    child = ExecutionId(
        parent_id=base_execution_id,
        name="child",
        sequence=0,
        args_hash="child",
    )
    backend = backend_factory("test-nested-child-commit")

    parent = TransactionScope(backend.transactions)
    await parent.__aenter__()
    await backend.executions.save(Execution.start(base_execution_id))

    child_scope = TransactionScope(backend.transactions)
    await child_scope.__aenter__()
    child_execution = Execution.start(child)
    child_execution.complete(_result("child"))
    await backend.executions.save(child_execution)
    await child_scope.commit()

    parent_record = await backend.executions.get(base_execution_id)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    child_record = await backend.executions.get(child)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    await parent.rollback()

    assert await backend.executions.get(base_execution_id) is None
    child_record = await backend.executions.get(child)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED


async def test_parent_staging_survives_nested_child_rollback(
    backend_factory: BackendFactory, base_execution_id: ExecutionId
):
    child = ExecutionId(
        parent_id=base_execution_id,
        name="child",
        sequence=0,
        args_hash="child",
    )
    backend = backend_factory("test-nested-child-rollback")

    parent = TransactionScope(backend.transactions)
    await parent.__aenter__()
    await backend.executions.save(Execution.start(base_execution_id))

    child_scope = TransactionScope(backend.transactions)
    await child_scope.__aenter__()
    child_execution = Execution.start(child)
    child_execution.complete(_result("child"))
    await backend.executions.save(child_execution)
    await child_scope.rollback()

    parent_record = await backend.executions.get(base_execution_id)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    assert await backend.executions.get(child) is None

    await parent.commit()

    parent_record = await backend.executions.get(base_execution_id)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    assert await backend.executions.get(child) is None


async def test_out_of_order_transaction_close_raises(
    backend_factory: BackendFactory, base_execution_id: ExecutionId
):
    backend = backend_factory("out-of-order")

    parent = await backend.transactions.begin_transaction()
    child = await backend.transactions.begin_transaction()

    with pytest.raises(RuntimeError, match="out of order"):
        await parent.commit()

    await child.rollback()
    await parent.rollback()


async def test_three_level_nested_transactions_restore_each_parent(
    backend_factory: BackendFactory,
):
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    child = ExecutionId(parent_id=root, name="child", sequence=0, args_hash="c")
    grandchild = ExecutionId(
        parent_id=child, name="grandchild", sequence=0, args_hash="g"
    )

    backend = backend_factory("three-level-nesting")

    root_scope = TransactionScope(backend.transactions)
    await root_scope.__aenter__()
    await backend.executions.save(Execution.start(root))

    child_scope = TransactionScope(backend.transactions)
    await child_scope.__aenter__()
    child_execution = Execution.start(child)

    grandchild_scope = TransactionScope(backend.transactions)
    await grandchild_scope.__aenter__()
    grandchild_execution = Execution.start(grandchild)
    grandchild_execution.complete(_result("grandchild"))
    await backend.executions.save(grandchild_execution)
    await grandchild_scope.commit()

    child_execution.complete(_result("child"))
    await backend.executions.save(child_execution)
    await child_scope.commit()

    await root_scope.rollback()

    assert await backend.executions.get(root) is None

    child_record = await backend.executions.get(child)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    grandchild_record = await backend.executions.get(grandchild)
    assert grandchild_record is not None
    assert grandchild_record.status == ExecutionStatus.COMPLETED


async def test_memory_backend_exposes_separate_repository_and_transactions(
    backend_factory: BackendFactory,
):
    backend = backend_factory("memory-boundary")

    assert isinstance(backend, MemoryBackend)
    assert backend.executions is not backend.transactions
    assert isinstance(backend.executions, ExecutionRepository)
    assert isinstance(backend.transactions, TransactionProvider)
    assert not hasattr(backend.executions, "serializer")
