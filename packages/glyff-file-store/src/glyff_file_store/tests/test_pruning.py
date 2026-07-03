from glyff import Execution, ExecutionId


async def test_descendants_of_returns_strict_descendants(backend_factory):
    backend = backend_factory("desc")
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    child_a = ExecutionId(parent_id=root, name="a", sequence=0, args_hash="a")
    child_b = ExecutionId(parent_id=root, name="b", sequence=0, args_hash="b")
    grand = ExecutionId(parent_id=child_a, name="grand", sequence=0, args_hash="g")

    tx = await backend.transactions.begin_transaction()
    for eid in (root, child_a, child_b, grand):
        await backend.executions.save(Execution.start(eid))
    await tx.commit()

    assert set(await backend.executions.descendants_of(root)) == {
        child_a,
        child_b,
        grand,
    }
    assert set(await backend.executions.descendants_of(child_a)) == {grand}
    assert await backend.executions.descendants_of(grand) == []


async def test_delete_many_removes_selected_records(backend_factory):
    backend = backend_factory("del")
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    child = ExecutionId(parent_id=root, name="child", sequence=0, args_hash="c")

    tx = await backend.transactions.begin_transaction()
    await backend.executions.save(Execution.start(root))
    await backend.executions.save(Execution.start(child))
    await tx.commit()

    tx = await backend.transactions.begin_transaction()
    await backend.executions.delete_many([child])
    await tx.commit()

    assert await backend.executions.get(child) is None
    assert await backend.executions.get(root) is not None
