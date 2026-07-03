import pytest

from glyff import Execution, ExecutionId, ExecutionStatus, SerializedValue, SessionStore
from glyff.tests.types import StoreFactory


def _result(value: str) -> SerializedValue:
    return SerializedValue(value.encode())


async def test_get_missing_returns_none(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-initial")
    assert await store.get(base_execution_id) is None


async def test_save_start_stages_execution(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-start")
    tx = await store.begin_transaction()
    await store.save(Execution.start(base_execution_id))
    staged = await store.get(base_execution_id)
    assert staged is not None
    assert staged.status == ExecutionStatus.STARTED
    await tx.commit()
    state = await store.get(base_execution_id)
    assert state is not None
    assert state.status == ExecutionStatus.STARTED


async def test_completed_result_persists(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-commit-ok")
    execution = Execution.start(base_execution_id)
    execution.complete(_result("42"))

    tx = await store.begin_transaction()
    await store.save(execution)
    staged = await store.get(base_execution_id)
    assert staged is not None
    assert staged.status == ExecutionStatus.COMPLETED
    assert staged.result == _result("42")
    await tx.commit()

    state = await store.get(base_execution_id)
    assert state is not None
    assert state.status == ExecutionStatus.COMPLETED
    assert state.result == _result("42")


async def test_failed_error_persists(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-commit-fail")
    execution = Execution.start(base_execution_id)
    execution.fail("something went wrong")
    tx = await store.begin_transaction()
    await store.save(execution)
    await tx.commit()

    state = await store.get(base_execution_id)
    assert state is not None
    assert state.status == ExecutionStatus.FAILED
    assert state.error == "something went wrong"


async def test_rollback_discards_save(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-rollback")
    tx = await store.begin_transaction()
    await store.save(Execution.start(base_execution_id))
    await tx.rollback()
    assert await store.get(base_execution_id) is None


async def test_save_requires_transaction(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-save-without-transaction")

    with pytest.raises(RuntimeError, match="write attempted outside a transaction"):
        await store.save(Execution.start(base_execution_id))


async def test_delete_many_requires_transaction(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-delete-without-transaction")

    with pytest.raises(RuntimeError, match="write attempted outside a transaction"):
        await store.delete_many([base_execution_id])


async def test_nested_child_commit_is_independent_of_parent_staging(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    child = ExecutionId(
        parent_id=base_execution_id,
        name="child",
        sequence=0,
        args_hash="child",
    )
    store: SessionStore = store_factory("test-nested-child-commit")

    parent_tx = await store.begin_transaction()
    await store.save(Execution.start(base_execution_id))

    child_tx = await store.begin_transaction()
    child_execution = Execution.start(child)
    child_execution.complete(_result("child"))
    await store.save(child_execution)
    await child_tx.commit()

    parent_record = await store.get(base_execution_id)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    child_record = await store.get(child)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    await parent_tx.rollback()

    assert await store.get(base_execution_id) is None
    child_record = await store.get(child)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED


async def test_parent_staging_survives_nested_child_rollback(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    child = ExecutionId(
        parent_id=base_execution_id,
        name="child",
        sequence=0,
        args_hash="child",
    )
    store: SessionStore = store_factory("test-nested-child-rollback")

    parent_tx = await store.begin_transaction()
    await store.save(Execution.start(base_execution_id))

    child_tx = await store.begin_transaction()
    child_execution = Execution.start(child)
    child_execution.complete(_result("child"))
    await store.save(child_execution)
    await child_tx.rollback()

    parent_record = await store.get(base_execution_id)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    assert await store.get(child) is None

    await parent_tx.commit()

    parent_record = await store.get(base_execution_id)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    assert await store.get(child) is None


async def test_out_of_order_transaction_close_raises(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("out-of-order")

    parent = await store.begin_transaction()
    child = await store.begin_transaction()

    with pytest.raises(RuntimeError, match="out of order"):
        await parent.commit()

    await child.rollback()
    await parent.rollback()


async def test_three_level_nested_transactions_restore_each_parent(
    store_factory: StoreFactory,
):
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    child = ExecutionId(parent_id=root, name="child", sequence=0, args_hash="c")
    grandchild = ExecutionId(
        parent_id=child, name="grandchild", sequence=0, args_hash="g"
    )

    store: SessionStore = store_factory("three-level-nesting")

    root_tx = await store.begin_transaction()
    await store.save(Execution.start(root))

    child_tx = await store.begin_transaction()
    child_execution = Execution.start(child)

    grandchild_tx = await store.begin_transaction()
    grandchild_execution = Execution.start(grandchild)
    grandchild_execution.complete(_result("grandchild"))
    await store.save(grandchild_execution)
    await grandchild_tx.commit()

    child_execution.complete(_result("child"))
    await store.save(child_execution)
    await child_tx.commit()

    await root_tx.rollback()

    assert await store.get(root) is None

    child_record = await store.get(child)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    grandchild_record = await store.get(grandchild)
    assert grandchild_record is not None
    assert grandchild_record.status == ExecutionStatus.COMPLETED
