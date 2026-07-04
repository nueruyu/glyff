from typing import cast

from glyff import Execution, ExecutionId, ExecutionStatus, SerializedValue
from glyff._context import TransactionScope
from glyff.store import MemoryBackend, MemoryExecutionRepository
from glyff.store._memory import _make_key
from glyff.store.utils import execution_id_to_path


async def _save(backend: MemoryBackend, execution: Execution) -> None:
    async with TransactionScope(backend.transaction_provider):
        await backend.repository.save(execution)


async def test_descendants_of_returns_strict_transitive_descendants(serializer):
    backend = MemoryBackend()
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    a = ExecutionId(parent_id=root, name="a", sequence=0, args_hash="a")
    b = ExecutionId(parent_id=root, name="b", sequence=0, args_hash="b")
    grand = ExecutionId(parent_id=a, name="grand", sequence=0, args_hash="g")

    for eid in (root, a, b, grand):
        await _save(backend, Execution.start(eid))

    assert set(await backend.repository.descendants_of(root)) == {a, b, grand}
    assert set(await backend.repository.descendants_of(a)) == {grand}
    assert await backend.repository.descendants_of(grand) == []


async def test_delete_many_removes_execution_parts(serializer):
    backend = MemoryBackend()
    eid = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    execution = Execution.start(eid)
    execution.complete(SerializedValue(await serializer.serialize("ok", str)))
    execution.set_metadata("k", SerializedValue(await serializer.serialize("v", str)))
    await _save(backend, execution)

    async with TransactionScope(backend.transaction_provider):
        await backend.repository.delete_many([eid])

    assert await backend.repository.get(eid) is None


async def test_descendants_ignore_metadata_only_orphans(serializer):
    backend = MemoryBackend()
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    child = ExecutionId(parent_id=root, name="child", sequence=0, args_hash="c")
    path = execution_id_to_path(child)
    repository = cast(MemoryExecutionRepository, backend.repository)
    repository._client.data[_make_key(path, "metadata")] = {"k": b'"v"'}

    assert await backend.repository.descendants_of(root) == []


async def test_delete_one_descendant_preserves_siblings(serializer):
    backend = MemoryBackend()
    root = ExecutionId(parent_id=None, name="root", sequence=0, args_hash="r")
    p1 = ExecutionId(parent_id=root, name="p1", sequence=0, args_hash="p1")
    p2 = ExecutionId(parent_id=root, name="p2", sequence=0, args_hash="p2")
    leaf1 = ExecutionId(parent_id=p1, name="leaf", sequence=0, args_hash="l1")
    leaf2 = ExecutionId(parent_id=p2, name="leaf", sequence=0, args_hash="l2")

    for eid in (root, p1, p2, leaf1, leaf2):
        execution = Execution.start(eid)
        execution.complete(SerializedValue(await serializer.serialize("ok", str)))
        await _save(backend, execution)

    async with TransactionScope(backend.transaction_provider):
        await backend.repository.delete_many([leaf1])

    assert await backend.repository.get(leaf1) is None
    leaf2_record = await backend.repository.get(leaf2)
    assert leaf2_record is not None
    assert leaf2_record.status is ExecutionStatus.COMPLETED
