"""Unit tests for the memory store's pruning mechanism and its full-path key
scheme (which also makes keys collision-free across different parents)."""

from glyff import ExecutionId
from glyff.stores import MemoryClient, MemorySessionStore


def _store(serializer) -> MemorySessionStore:
    return MemorySessionStore(client=MemoryClient(), serializer=serializer)


def _child(parent, name, seq=0, h="h0") -> ExecutionId:
    return ExecutionId(parent_id=parent, name=name, sequence=seq, args_hash=h)


async def _complete(
    store: MemorySessionStore, eid: ExecutionId, value, rtype=str
) -> None:
    tx = await store.begin_transaction()
    ex = await store.start_execution(eid)
    await ex.complete(value, rtype)
    await tx.commit()


async def test_get_descendants_strict_and_transitive(serializer):
    store = _store(serializer)
    root = _child(None, "root")
    a = _child(root, "a")
    grand = _child(a, "grand")
    b = _child(root, "b")
    for eid in (root, a, grand, b):
        await _complete(store, eid, "v")

    assert set(await store.get_descendants(root)) == {a, grand, b}
    assert set(await store.get_descendants(a)) == {grand}
    assert await store.get_descendants(grand) == []


async def test_delete_execution_removes_only_that_id(serializer):
    store = _store(serializer)
    root = _child(None, "root")
    child = _child(root, "child")
    await _complete(store, root, "rv")
    await _complete(store, child, "cv")

    tx = await store.begin_transaction()
    await store.delete_executions([child])
    await tx.commit()

    assert await store.get_execution_record(child, str) is None
    root_rec = await store.get_execution_record(root, str)
    assert root_rec is not None and root_rec.result == "rv"


async def test_delete_execution_rolls_back(serializer):
    store = _store(serializer)
    eid = _child(None, "root")
    await _complete(store, eid, "v")

    tx = await store.begin_transaction()
    await store.delete_executions([eid])
    await tx.rollback()

    rec = await store.get_execution_record(eid, str)
    assert rec is not None and rec.result == "v"


async def test_full_path_keys_avoid_cross_parent_collision(serializer):
    """Two children with identical (name, sequence, args_hash) under different
    parents must remain independent records (the old flat key scheme collided)."""
    store = _store(serializer)
    p1 = _child(None, "p1")
    p2 = _child(None, "p2")
    leaf_under_p1 = _child(p1, "leaf", 0, "samehash")
    leaf_under_p2 = _child(p2, "leaf", 0, "samehash")

    await _complete(store, leaf_under_p1, "one")
    await _complete(store, leaf_under_p2, "two")

    assert (await store.get_execution_record(leaf_under_p1, str)).result == "one"
    assert (await store.get_execution_record(leaf_under_p2, str)).result == "two"

    # Deleting one leaf does not touch the colliding sibling under the other parent.
    tx = await store.begin_transaction()
    await store.delete_executions([leaf_under_p1])
    await tx.commit()
    assert await store.get_execution_record(leaf_under_p1, str) is None
    assert (await store.get_execution_record(leaf_under_p2, str)).result == "two"


async def test_all_keys_includes_staged_excludes_deleted():
    client = MemoryClient()
    client.stage_write("a", 1)
    await client.commit_staged()
    client.stage_write("b", 2)
    client.stage_delete("a")
    # "a" is committed but staged for deletion; "b" is staged for write.
    assert client.all_keys() == {"b"}
