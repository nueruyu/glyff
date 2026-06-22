import asyncio
from pathlib import Path

from glyff import ExecutionId, ExecutionStatus
from glyff.serialization import JsonSerializer
from glyff.store.utils import execution_id_to_path
from glyff_file_store import SQLiteSessionStore


def make_execution_id(
    name: str,
    sequence: int = 0,
    args_hash: str = "args",
    parent_id: ExecutionId | None = None,
) -> ExecutionId:
    return ExecutionId(
        parent_id=parent_id,
        name=name,
        sequence=sequence,
        args_hash=args_hash,
    )


def make_store(tmp_path: Path) -> SQLiteSessionStore:
    return SQLiteSessionStore(tmp_path / "executions.sqlite3", JsonSerializer())


async def test_sqlite_store_persists_completed_execution(tmp_path: Path):
    execution_id = make_execution_id("root")
    store = make_store(tmp_path)

    transaction = await store.begin_transaction()
    execution = await store.start_execution(execution_id)
    await execution.complete({"answer": 42}, dict)
    await transaction.commit()

    reloaded = make_store(tmp_path)
    record = await reloaded.get_execution_record(execution_id, dict)

    assert record is not None
    assert record.status == ExecutionStatus.COMPLETED
    assert record.result == {"answer": 42}
    assert record.error is None


async def test_sqlite_store_persists_failed_execution(tmp_path: Path):
    execution_id = make_execution_id("root")
    store = make_store(tmp_path)

    transaction = await store.begin_transaction()
    execution = await store.start_execution(execution_id)
    await execution.fail("boom")
    await transaction.commit()

    record = await store.get_execution_record(execution_id, object)

    assert record is not None
    assert record.status == ExecutionStatus.FAILED
    assert record.result is None
    assert record.error == "boom"


async def test_sqlite_store_rolls_back_staged_execution(tmp_path: Path):
    execution_id = make_execution_id("root")
    store = make_store(tmp_path)

    transaction = await store.begin_transaction()
    execution = await store.start_execution(execution_id)
    await execution.complete("rolled back", str)
    await transaction.rollback()

    assert await store.get_execution_record(execution_id, str) is None


async def test_sqlite_store_returns_descendants(tmp_path: Path):
    parent = make_execution_id("parent")
    child_a = make_execution_id("child", sequence=0, args_hash="a", parent_id=parent)
    child_b = make_execution_id("child", sequence=1, args_hash="b", parent_id=parent)
    unrelated = make_execution_id("unrelated")
    store = make_store(tmp_path)

    transaction = await store.begin_transaction()
    for execution_id in [parent, child_a, child_b, unrelated]:
        execution = await store.start_execution(execution_id)
        await execution.complete(execution_id.name, str)
    await transaction.commit()

    descendants = await store.get_descendants(parent)

    assert {execution_id_to_path(eid) for eid in descendants} == {
        execution_id_to_path(child_a),
        execution_id_to_path(child_b),
    }


async def test_sqlite_store_deletes_committed_executions(tmp_path: Path):
    parent = make_execution_id("parent")
    child = make_execution_id("child", parent_id=parent)
    store = make_store(tmp_path)

    transaction = await store.begin_transaction()
    for execution_id in [parent, child]:
        execution = await store.start_execution(execution_id)
        await execution.complete(execution_id.name, str)
    await transaction.commit()

    delete_transaction = await store.begin_transaction()
    await store.delete_executions([child])
    assert await store.get_descendants(parent) == []
    await delete_transaction.commit()

    assert await store.get_execution_record(child, str) is None
    assert await store.get_descendants(parent) == []


async def test_sqlite_store_handles_parallel_completions(tmp_path: Path):
    parent = make_execution_id("parent")
    children = [
        make_execution_id("child", sequence=i, args_hash=str(i), parent_id=parent)
        for i in range(20)
    ]
    store = make_store(tmp_path)

    transaction = await store.begin_transaction()
    executions = await asyncio.gather(
        *(store.start_execution(child) for child in children)
    )
    await asyncio.gather(
        *(execution.complete(i, int) for i, execution in enumerate(executions))
    )
    await transaction.commit()

    for i, child in enumerate(children):
        record = await store.get_execution_record(child, int)
        assert record is not None
        assert record.status == ExecutionStatus.COMPLETED
        assert record.result == i
