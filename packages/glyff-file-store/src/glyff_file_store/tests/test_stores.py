import pytest
from glyff import ExecutionId
from glyff import SessionStore
from glyff import ExecutionStatus


async def test_initial_state_is_none(store_factory, base_execution_id: ExecutionId):
    store: SessionStore = store_factory("test-initial")
    assert await store.get_execution_record(base_execution_id, dict) is None


async def test_start_execution_stages_start_event(
    store_factory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-start")
    tx = await store.begin_transaction()
    await store.start_execution(base_execution_id)
    assert await store.get_execution_record(base_execution_id, dict) is None
    await tx.commit()
    state = await store.get_execution_record(base_execution_id, dict)
    assert state is not None
    assert state.status == ExecutionStatus.STARTED


async def test_commit_completion(store_factory, base_execution_id: ExecutionId):
    store: SessionStore = store_factory("test-commit-ok")
    result_obj = {"result": 42}

    tx = await store.begin_transaction()
    execution = await store.start_execution(base_execution_id)
    await execution.complete(result_obj, dict)
    assert await store.get_execution_record(base_execution_id, dict) is None
    await tx.commit()

    state = await store.get_execution_record(base_execution_id, dict)
    assert state is not None
    assert state.status == ExecutionStatus.COMPLETED
    assert state.result == result_obj


async def test_commit_failure(store_factory, base_execution_id: ExecutionId):
    store: SessionStore = store_factory("test-commit-fail")
    tx = await store.begin_transaction()
    execution = await store.start_execution(base_execution_id)
    await execution.fail("something went wrong")
    await tx.commit()

    state = await store.get_execution_record(base_execution_id, str)
    assert state is not None
    assert state.status == ExecutionStatus.FAILED
    assert state.error == "something went wrong"


async def test_rollback_discards_staged(store_factory, base_execution_id: ExecutionId):
    store: SessionStore = store_factory("test-rollback")
    tx = await store.begin_transaction()
    execution = await store.start_execution(base_execution_id)
    await execution.complete({"result": 1}, dict)
    await tx.rollback()
    assert await store.get_execution_record(base_execution_id, dict) is None


async def test_failed_commit_does_not_advance_state(
    store_factory, base_execution_id: ExecutionId
):
    """If the underlying client write fails, the transaction's post-commit
    hook must not run — so the in-memory state stays where it was before the
    commit attempt."""
    store: SessionStore = store_factory("test-atomicity")
    tx = await store.begin_transaction()
    execution = await store.start_execution(base_execution_id)
    await execution.complete({"value": 42}, dict)

    # Sanity check: state is not visible before commit.
    assert await store.get_execution_record(base_execution_id, dict) is None

    # Force the client commit to fail.
    async def boom() -> None:
        raise RuntimeError("simulated disk failure")

    original_commit = store._client.commit_staged  # type: ignore[attr-defined]
    store._client.commit_staged = boom  # type: ignore[attr-defined]
    try:
        with pytest.raises(RuntimeError, match="simulated disk failure"):
            await tx.commit()
    finally:
        store._client.commit_staged = original_commit  # type: ignore[attr-defined]

    # In-memory state must not have advanced.
    assert await store.get_execution_record(base_execution_id, dict) is None
