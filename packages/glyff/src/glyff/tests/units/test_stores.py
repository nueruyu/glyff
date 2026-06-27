import pytest

from glyff import ExecutionId, ExecutionStatus, SessionStore
from glyff.tests.types import StoreFactory


async def test_initial_state_is_none(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-initial")
    assert await store.get_execution_record(base_execution_id, dict) is None


async def test_start_execution_stages_start_event(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-start")
    tx = await store.begin_transaction()
    await store.start_execution(base_execution_id)
    # Read-your-writes: the staged STARTED state is visible within the
    # transaction, before commit.
    staged = await store.get_execution_record(base_execution_id, dict)
    assert staged is not None
    assert staged.status == ExecutionStatus.STARTED
    await tx.commit()
    state = await store.get_execution_record(base_execution_id, dict)
    assert state is not None
    assert state.status == ExecutionStatus.STARTED


async def test_commit_completion(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-commit-ok")
    result_obj = {"result": 42}

    tx = await store.begin_transaction()
    execution = await store.start_execution(base_execution_id)
    await execution.complete(result_obj, dict)
    # Read-your-writes: the staged completion is visible before commit.
    staged = await store.get_execution_record(base_execution_id, dict)
    assert staged is not None
    assert staged.status == ExecutionStatus.COMPLETED
    assert staged.result == result_obj
    await tx.commit()

    state = await store.get_execution_record(base_execution_id, dict)
    assert state is not None
    assert state.status == ExecutionStatus.COMPLETED
    assert state.result == result_obj


async def test_commit_failure(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-commit-fail")
    tx = await store.begin_transaction()
    execution = await store.start_execution(base_execution_id)
    await execution.fail("something went wrong")
    await tx.commit()

    state = await store.get_execution_record(base_execution_id, str)
    assert state is not None
    assert state.status == ExecutionStatus.FAILED
    assert state.error == "something went wrong"


async def test_rollback_discards_staged(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-rollback")
    tx = await store.begin_transaction()
    execution = await store.start_execution(base_execution_id)
    await execution.complete({"result": 1}, dict)
    await tx.rollback()
    assert await store.get_execution_record(base_execution_id, dict) is None


async def test_start_execution_requires_transaction(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-start-without-transaction")

    with pytest.raises(RuntimeError, match="write attempted outside a transaction"):
        await store.start_execution(base_execution_id)


async def test_delete_executions_requires_transaction(
    store_factory: StoreFactory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-delete-without-transaction")

    with pytest.raises(RuntimeError, match="write attempted outside a transaction"):
        await store.delete_executions([base_execution_id])


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
    await store.start_execution(base_execution_id)

    child_tx = await store.begin_transaction()
    child_execution = await store.start_execution(child)
    await child_execution.complete("child", str)
    await child_tx.commit()

    parent_record = await store.get_execution_record(base_execution_id, str)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    child_record = await store.get_execution_record(child, str)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    await parent_tx.rollback()

    assert await store.get_execution_record(base_execution_id, str) is None
    child_record = await store.get_execution_record(child, str)
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
    await store.start_execution(base_execution_id)

    child_tx = await store.begin_transaction()
    child_execution = await store.start_execution(child)
    await child_execution.complete("child", str)
    await child_tx.rollback()

    parent_record = await store.get_execution_record(base_execution_id, str)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    assert await store.get_execution_record(child, str) is None

    await parent_tx.commit()

    parent_record = await store.get_execution_record(base_execution_id, str)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    assert await store.get_execution_record(child, str) is None


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
    await store.start_execution(root)

    child_tx = await store.begin_transaction()
    child_execution = await store.start_execution(child)

    grandchild_tx = await store.begin_transaction()
    grandchild_execution = await store.start_execution(grandchild)
    await grandchild_execution.complete("grandchild", str)
    await grandchild_tx.commit()

    await child_execution.complete("child", str)
    await child_tx.commit()

    await root_tx.rollback()

    assert await store.get_execution_record(root, str) is None

    child_record = await store.get_execution_record(child, str)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    grandchild_record = await store.get_execution_record(grandchild, str)
    assert grandchild_record is not None
    assert grandchild_record.status == ExecutionStatus.COMPLETED
