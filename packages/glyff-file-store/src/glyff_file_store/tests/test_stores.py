import pytest
from glyff import Execution, ExecutionId, ExecutionStatus, SerializedValue, SessionStore
from glyff_file_store import JsonFileSessionStore


async def test_get_missing_returns_none(store_factory, base_execution_id: ExecutionId):
    store: SessionStore = store_factory("test-initial")
    assert await store.get(base_execution_id) is None


async def test_save_start_persists(store_factory, base_execution_id: ExecutionId):
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
    store_factory, base_execution_id: ExecutionId, serializer
):
    store: SessionStore = store_factory("test-completed")
    execution = Execution.start(base_execution_id)
    execution.complete(SerializedValue(await serializer.serialize({"result": 42}, dict)))
    tx = await store.begin_transaction()
    await store.save(execution)
    await tx.commit()

    state = await store.get(base_execution_id)
    assert state is not None
    assert state.status == ExecutionStatus.COMPLETED
    assert state.result is not None
    assert await serializer.deserialize(state.result.data, dict) == {"result": 42}


async def test_failed_error_persists(store_factory, base_execution_id: ExecutionId):
    store: SessionStore = store_factory("test-failed")
    execution = Execution.start(base_execution_id)
    execution.fail("boom")
    tx = await store.begin_transaction()
    await store.save(execution)
    await tx.commit()

    state = await store.get(base_execution_id)
    assert state is not None
    assert state.status == ExecutionStatus.FAILED
    assert state.error == "boom"


async def test_metadata_persists_as_part_of_execution(
    store_factory, base_execution_id: ExecutionId, serializer
):
    store: SessionStore = store_factory("test-meta")
    execution = Execution.start(base_execution_id)
    execution.set_metadata("a", SerializedValue(await serializer.serialize("one", str)))
    execution.set_metadata("b", SerializedValue(await serializer.serialize("two", str)))
    execution.set_metadata("a", SerializedValue(await serializer.serialize("ONE", str)))
    tx = await store.begin_transaction()
    await store.save(execution)
    await tx.commit()

    state = await store.get(base_execution_id)
    assert state is not None
    assert set(state.metadata) == {"a", "b"}
    assert await serializer.deserialize(state.metadata["a"].value.data, str) == "ONE"
    assert await serializer.deserialize(state.metadata["b"].value.data, str) == "two"


async def test_rollback_discards_save(store_factory, base_execution_id: ExecutionId):
    store: SessionStore = store_factory("test-rollback")
    tx = await store.begin_transaction()
    await store.save(Execution.start(base_execution_id))
    await tx.rollback()
    assert await store.get(base_execution_id) is None


async def test_save_requires_transaction(store_factory, base_execution_id: ExecutionId):
    store: SessionStore = store_factory("test-save-without-transaction")
    with pytest.raises(RuntimeError, match="write attempted outside a transaction"):
        await store.save(Execution.start(base_execution_id))


async def test_delete_many_requires_transaction(
    store_factory, base_execution_id: ExecutionId
):
    store: SessionStore = store_factory("test-delete-without-transaction")
    with pytest.raises(RuntimeError, match="write attempted outside a transaction"):
        await store.delete_many([base_execution_id])


async def test_descendants_and_delete_many(store_factory):
    store: SessionStore = store_factory("test-desc")
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    child = ExecutionId(parent_id=root, name="child", sequence=0, args_hash="c")
    grand = ExecutionId(parent_id=child, name="grand", sequence=0, args_hash="g")
    tx = await store.begin_transaction()
    for eid in (root, child, grand):
        await store.save(Execution.start(eid))
    await tx.commit()

    assert set(await store.descendants_of(root)) == {child, grand}

    tx = await store.begin_transaction()
    await store.delete_many([child])
    await tx.commit()

    assert await store.get(child) is None
    assert await store.get(grand) is not None


async def test_constructor_returns_repository(store_factory):
    assert isinstance(store_factory("factory"), JsonFileSessionStore)
