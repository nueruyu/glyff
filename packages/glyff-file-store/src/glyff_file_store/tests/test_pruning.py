"""Unit tests for the file store's pruning *mechanism*: the read-only
``get_descendants`` structural query and the batched ``delete_executions``.
Pruning *policy* (when to call these) lives in the executor and is covered by
the scenario tests."""

from glyff import ExecutionId
from glyff import ExecutionStatus

from glyff_file_store import JsonFileSessionStore


def _child(
    parent: ExecutionId | None, name: str, seq: int = 0, h: str = "h0"
) -> ExecutionId:
    return ExecutionId(parent_id=parent, name=name, sequence=seq, args_hash=h)


async def _complete(
    store: JsonFileSessionStore, eid: ExecutionId, value, rtype=str
) -> None:
    tx = await store.begin_transaction()
    ex = await store.start_execution(eid)
    await ex.complete(value, rtype)
    await tx.commit()


async def _fail(
    store: JsonFileSessionStore, eid: ExecutionId, error: str = "boom"
) -> None:
    tx = await store.begin_transaction()
    ex = await store.start_execution(eid)
    await ex.fail(error)
    await tx.commit()


async def test_callstack_id_roundtrip(store_factory, base_execution_id: ExecutionId):
    store: JsonFileSessionStore = store_factory("roundtrip")
    nested = _child(_child(base_execution_id, "mid", 2, "abc"), "leaf", 5, "def456")
    call_stack = store._id_to_callstack(nested)
    assert store._callstack_to_id(call_stack) == nested


async def test_get_descendants_returns_strict_descendants(store_factory):
    store: JsonFileSessionStore = store_factory("desc")
    root = _child(None, "root")
    child_a = _child(root, "child_a")
    grand = _child(child_a, "grand")
    child_b = _child(root, "child_b")

    for eid in (root, child_a, grand, child_b):
        await _complete(store, eid, "v")

    # Strict: root itself is excluded; the whole subtree (transitive) is returned.
    assert set(await store.get_descendants(root)) == {child_a, grand, child_b}
    # Reconstructed ids round-trip exactly to the original keys.
    assert set(await store.get_descendants(child_a)) == {grand}
    assert await store.get_descendants(grand) == []


async def test_get_descendants_includes_failed_children(store_factory):
    store: JsonFileSessionStore = store_factory("desc-failed")
    root = _child(None, "root")
    bad = _child(root, "bad")
    await _complete(store, root, "v")
    await _fail(store, bad)

    # A failed child of a completed parent is unreachable on replay, so the
    # query still surfaces it for deletion.
    assert set(await store.get_descendants(root)) == {bad}


async def test_delete_execution_removes_only_that_id_and_reindexes(store_factory):
    store: JsonFileSessionStore = store_factory("del")
    root = _child(None, "root")
    child = _child(root, "child")

    # Record the child *before* the root so the child's entries sit at lower
    # indices; deleting them shifts the root's entry positions and would point
    # a stale index at the wrong row if the rebuild were skipped.
    await _complete(store, child, "child_val")
    await _complete(store, root, "root_val")

    tx = await store.begin_transaction()
    await store.delete_executions([child])
    # Not visible until commit.
    assert (await store.get_execution_record(child, str)) is not None
    await tx.commit()

    assert await store.get_execution_record(child, str) is None
    root_rec = await store.get_execution_record(root, str)
    assert root_rec is not None
    assert root_rec.status == ExecutionStatus.COMPLETED
    assert root_rec.result == "root_val"


async def test_delete_execution_rolls_back(store_factory):
    store: JsonFileSessionStore = store_factory("del-rollback")
    eid = _child(None, "root")
    await _complete(store, eid, "v")

    tx = await store.begin_transaction()
    await store.delete_executions([eid])
    await tx.rollback()

    rec = await store.get_execution_record(eid, str)
    assert rec is not None
    assert rec.result == "v"


async def test_deleted_entry_excluded_from_disk(store_factory, tmp_path, serializer):
    """After a committed delete, a fresh store reading the same file must not
    see the deleted entry."""
    from glyff_file_store import FileClient

    store: JsonFileSessionStore = store_factory("del-disk")
    root = _child(None, "root")
    child = _child(root, "child")
    await _complete(store, root, "rv")
    await _complete(store, child, "cv")

    tx = await store.begin_transaction()
    await store.delete_executions([child])
    await tx.commit()

    reopened = JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id="del-disk"),
        serializer=serializer,
    )
    assert await reopened.get_execution_record(child, str) is None
    root_rec = await reopened.get_execution_record(root, str)
    assert root_rec is not None
    assert root_rec.result == "rv"
