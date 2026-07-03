from glyff import Execution, ExecutionId


async def test_descendants_of_returns_strict_descendants(store_factory):
    store = store_factory("desc")
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    child_a = ExecutionId(parent_id=root, name="a", sequence=0, args_hash="a")
    child_b = ExecutionId(parent_id=root, name="b", sequence=0, args_hash="b")
    grand = ExecutionId(parent_id=child_a, name="grand", sequence=0, args_hash="g")

    tx = await store.begin_transaction()
    for eid in (root, child_a, child_b, grand):
        await store.save(Execution.start(eid))
    await tx.commit()

    assert set(await store.descendants_of(root)) == {child_a, child_b, grand}
    assert set(await store.descendants_of(child_a)) == {grand}
    assert await store.descendants_of(grand) == []


async def test_delete_many_removes_selected_records(store_factory):
    store = store_factory("del")
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    child = ExecutionId(parent_id=root, name="child", sequence=0, args_hash="c")

    tx = await store.begin_transaction()
    await store.save(Execution.start(root))
    await store.save(Execution.start(child))
    await tx.commit()

    tx = await store.begin_transaction()
    await store.delete_many([child])
    await tx.commit()

    assert await store.get(child) is None
    assert await store.get(root) is not None
