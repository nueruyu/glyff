from glyff import Execution, ExecutionId, ExecutionStatus, SerializedValue
from glyff.store import MemorySessionStore
from glyff.store._memory import _make_key
from glyff.store._memory_client import MemoryClient
from glyff.store.utils import execution_id_to_path


def _store(serializer) -> MemorySessionStore:
    return MemorySessionStore(client=MemoryClient(), serializer=serializer)


async def _save(store: MemorySessionStore, execution: Execution) -> None:
    tx = await store.begin_transaction()
    await store.save(execution)
    await tx.commit()


async def test_descendants_of_returns_strict_transitive_descendants(serializer):
    store = _store(serializer)
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    a = ExecutionId(parent_id=root, name="a", sequence=0, args_hash="a")
    b = ExecutionId(parent_id=root, name="b", sequence=0, args_hash="b")
    grand = ExecutionId(parent_id=a, name="grand", sequence=0, args_hash="g")

    for eid in (root, a, b, grand):
        await _save(store, Execution.start(eid))

    assert set(await store.descendants_of(root)) == {a, b, grand}
    assert set(await store.descendants_of(a)) == {grand}
    assert await store.descendants_of(grand) == []


async def test_delete_many_removes_execution_parts(serializer):
    store = _store(serializer)
    eid = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    execution = Execution.start(eid)
    execution.complete(SerializedValue(await serializer.serialize("ok", str)))
    execution.set_metadata("k", SerializedValue(await serializer.serialize("v", str)))
    await _save(store, execution)

    tx = await store.begin_transaction()
    await store.delete_many([eid])
    await tx.commit()

    assert await store.get(eid) is None


async def test_descendants_ignore_metadata_only_orphans(serializer):
    store = _store(serializer)
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    child = ExecutionId(parent_id=root, name="child", sequence=0, args_hash="c")
    path = execution_id_to_path(child)
    store._client.data[_make_key(path, "metadata")] = {"k": b'"v"'}

    assert await store.descendants_of(root) == []


async def test_delete_one_descendant_preserves_siblings(serializer):
    store = _store(serializer)
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    p1 = ExecutionId(parent_id=root, name="p1", sequence=0, args_hash="p1")
    p2 = ExecutionId(parent_id=root, name="p2", sequence=0, args_hash="p2")
    leaf1 = ExecutionId(parent_id=p1, name="leaf", sequence=0, args_hash="l1")
    leaf2 = ExecutionId(parent_id=p2, name="leaf", sequence=0, args_hash="l2")

    for eid in (root, p1, p2, leaf1, leaf2):
        execution = Execution.start(eid)
        execution.complete(SerializedValue(await serializer.serialize("ok", str)))
        await _save(store, execution)

    tx = await store.begin_transaction()
    await store.delete_many([leaf1])
    await tx.commit()

    assert await store.get(leaf1) is None
    leaf2_record = await store.get(leaf2)
    assert leaf2_record is not None
    assert leaf2_record.status is ExecutionStatus.COMPLETED
