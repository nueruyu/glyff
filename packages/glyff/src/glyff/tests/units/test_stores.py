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
