from typing import cast

from glyff import Execution, ExecutionStatus, SerializedValue
from glyff.testing import encoded_args, eid
from glyff._context import TransactionScope
from glyff.store import MemoryBackend, MemoryExecutionRepository
from glyff.store._memory import _make_key
from glyff.store.utils import execution_id_to_path


async def _save(backend: MemoryBackend, execution: Execution) -> None:
    async with TransactionScope(backend.transaction_provider):
        await backend.repository.save(execution)


async def test_descendants_of_returns_strict_transitive_descendants(serializer):
    backend = MemoryBackend()
    root = eid("root")
    a = eid("a", parent=root)
    b = eid("b", parent=root)
    grand = eid("grand", parent=a)

    for execution_id in (root, a, b, grand):
        await _save(backend, Execution.start(execution_id, encoded_args()))

    assert set(await backend.repository.descendants_of(root)) == {a, b, grand}
    assert set(await backend.repository.descendants_of(a)) == {grand}
    assert await backend.repository.descendants_of(grand) == []


async def test_delete_many_removes_execution_parts(serializer):
    backend = MemoryBackend()
    execution_id = eid("root")
    execution = Execution.start(execution_id, encoded_args())
    execution.complete(SerializedValue(await serializer.serialize("ok", str)))
    execution.set_metadata("k", SerializedValue(await serializer.serialize("v", str)))
    await _save(backend, execution)

    async with TransactionScope(backend.transaction_provider):
        await backend.repository.delete_many([execution_id])

    assert await backend.repository.get(execution_id) is None


async def test_descendants_ignore_metadata_only_orphans(serializer):
    backend = MemoryBackend()
    root = eid("root")
    child = eid("child", parent=root)
    path = execution_id_to_path(child)
    repository = cast(MemoryExecutionRepository, backend.repository)
    repository._client.data[_make_key(path, "metadata")] = {"k": b'"v"'}

    assert await backend.repository.descendants_of(root) == []


async def test_delete_one_descendant_preserves_siblings(serializer):
    backend = MemoryBackend()
    root = eid("root")
    p1 = eid("p1", parent=root)
    p2 = eid("p2", parent=root)
    leaf1 = eid("leaf", parent=p1)
    leaf2 = eid("leaf", parent=p2)

    for execution_id in (root, p1, p2, leaf1, leaf2):
        execution = Execution.start(execution_id, encoded_args())
        execution.complete(SerializedValue(await serializer.serialize("ok", str)))
        await _save(backend, execution)

    async with TransactionScope(backend.transaction_provider):
        await backend.repository.delete_many([leaf1])

    assert await backend.repository.get(leaf1) is None
    leaf2_record = await backend.repository.get(leaf2)
    assert leaf2_record is not None
    assert leaf2_record.status is ExecutionStatus.COMPLETED
