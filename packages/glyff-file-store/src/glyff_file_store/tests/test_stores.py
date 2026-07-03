import pytest
from glyff import (
    Execution,
    ExecutionId,
    ExecutionRepository,
    ExecutionStatus,
    SerializedValue,
    TransactionProvider,
    TransactionScope,
)
from glyff_file_store import JsonFileBackend


async def test_get_missing_returns_none(
    backend_factory, base_execution_id: ExecutionId
):
    backend = backend_factory("test-initial")
    assert await backend.executions.get(base_execution_id) is None


async def test_save_start_persists(backend_factory, base_execution_id: ExecutionId):
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
    backend_factory, base_execution_id: ExecutionId, serializer
):
    backend = backend_factory("test-completed")
    execution = Execution.start(base_execution_id)
    execution.complete(
        SerializedValue(await serializer.serialize({"result": 42}, dict))
    )

    async with TransactionScope(backend.transactions):
        await backend.executions.save(execution)

    state = await backend.executions.get(base_execution_id)
    assert state is not None
    assert state.status == ExecutionStatus.COMPLETED
    assert state.result is not None
    assert await serializer.deserialize(state.result.data, dict) == {"result": 42}


async def test_failed_error_persists(backend_factory, base_execution_id: ExecutionId):
    backend = backend_factory("test-failed")
    execution = Execution.start(base_execution_id)
    execution.fail("boom")

    async with TransactionScope(backend.transactions):
        await backend.executions.save(execution)

    state = await backend.executions.get(base_execution_id)
    assert state is not None
    assert state.status == ExecutionStatus.FAILED
    assert state.error == "boom"


async def test_metadata_persists_as_part_of_execution(
    backend_factory, base_execution_id: ExecutionId, serializer
):
    backend = backend_factory("test-meta")
    execution = Execution.start(base_execution_id)
    execution.set_metadata("a", SerializedValue(await serializer.serialize("one", str)))
    execution.set_metadata("b", SerializedValue(await serializer.serialize("two", str)))
    execution.set_metadata("a", SerializedValue(await serializer.serialize("ONE", str)))

    async with TransactionScope(backend.transactions):
        await backend.executions.save(execution)

    state = await backend.executions.get(base_execution_id)
    assert state is not None
    assert set(state.metadata) == {"a", "b"}
    assert await serializer.deserialize(state.metadata["a"].value.data, str) == "ONE"
    assert await serializer.deserialize(state.metadata["b"].value.data, str) == "two"


async def test_binary_serialized_values_persist_after_reopen(
    backend_factory, base_execution_id: ExecutionId
):
    data = b"\xff\xfe\x00binary"
    backend = backend_factory("test-binary")
    execution = Execution.start(base_execution_id)
    execution.complete(SerializedValue(data))
    execution.set_metadata("trace", SerializedValue(data))

    async with TransactionScope(backend.transactions):
        await backend.executions.save(execution)

    reopened = backend_factory("test-binary")
    state = await reopened.executions.get(base_execution_id)

    assert state is not None
    assert state.result == SerializedValue(data)
    assert state.metadata["trace"].value == SerializedValue(data)


async def test_rollback_discards_save(backend_factory, base_execution_id: ExecutionId):
    backend = backend_factory("test-rollback")
    scope = TransactionScope(backend.transactions)
    await scope.__aenter__()
    await backend.executions.save(Execution.start(base_execution_id))
    await scope.__aexit__(RuntimeError, RuntimeError("boom"), None)

    assert await backend.executions.get(base_execution_id) is None


async def test_save_requires_transaction(
    backend_factory, base_execution_id: ExecutionId
):
    backend = backend_factory("test-save-without-transaction")
    with pytest.raises(RuntimeError, match="write attempted outside a transaction"):
        await backend.executions.save(Execution.start(base_execution_id))


async def test_delete_many_requires_transaction(
    backend_factory, base_execution_id: ExecutionId
):
    backend = backend_factory("test-delete-without-transaction")
    with pytest.raises(RuntimeError, match="write attempted outside a transaction"):
        await backend.executions.delete_many([base_execution_id])


async def test_descendants_and_delete_many(backend_factory):
    backend = backend_factory("test-desc")
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    child = ExecutionId(parent_id=root, name="child", sequence=0, args_hash="c")
    grand = ExecutionId(parent_id=child, name="grand", sequence=0, args_hash="g")

    async with TransactionScope(backend.transactions):
        for eid in (root, child, grand):
            await backend.executions.save(Execution.start(eid))

    assert set(await backend.executions.descendants_of(root)) == {child, grand}

    async with TransactionScope(backend.transactions):
        await backend.executions.delete_many([child])

    assert await backend.executions.get(child) is None
    assert await backend.executions.get(grand) is not None


async def test_backend_exposes_separate_repository_and_transactions(backend_factory):
    backend = backend_factory("factory")

    assert isinstance(backend, JsonFileBackend)
    assert backend.executions is not backend.transactions
    assert isinstance(backend.executions, ExecutionRepository)
    assert isinstance(backend.transactions, TransactionProvider)
    assert not hasattr(backend.executions, "serializer")
