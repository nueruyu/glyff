import asyncio
import threading
from pathlib import Path

import pytest
from glyff import ExecutionId, ExecutionStatus
from glyff.serialization import JsonSerializer
from glyff.store.utils import execution_id_to_path

from glyff_file_store import SQLiteSessionStore
from glyff_file_store._sqlite_store import _SQLiteTransaction


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


async def test_multiple_writes_in_one_transaction_commit_atomically(tmp_path: Path):
    # Several executions staged in a single transaction become durable together
    # on commit — the basis for multiple stores / external code sharing one
    # transaction.
    a = make_execution_id("a", args_hash="a")
    b = make_execution_id("b", args_hash="b")
    store = make_store(tmp_path)

    transaction = await store.begin_transaction()
    for eid in (a, b):
        execution = await store.start_execution(eid)
        await execution.complete(eid.name, str)
    await transaction.commit()

    reloaded = make_store(tmp_path)
    a_record = await reloaded.get_execution_record(a, str)
    b_record = await reloaded.get_execution_record(b, str)
    assert a_record is not None
    assert b_record is not None
    assert a_record.result == "a"
    assert b_record.result == "b"


async def test_multiple_writes_in_one_transaction_roll_back_atomically(tmp_path: Path):
    a = make_execution_id("a", args_hash="a")
    b = make_execution_id("b", args_hash="b")
    store = make_store(tmp_path)

    transaction = await store.begin_transaction()
    for eid in (a, b):
        execution = await store.start_execution(eid)
        await execution.complete(eid.name, str)
    await transaction.rollback()

    assert await store.get_execution_record(a, str) is None
    assert await store.get_execution_record(b, str) is None


async def test_sqlite_transaction_concurrent_close_finishes_once():
    class FakeStore:
        def __init__(self):
            self.end_calls = 0

        def _end_transaction(self, token) -> None:
            self.end_calls += 1

    store = FakeStore()
    transaction = _SQLiteTransaction(store, object())  # type: ignore[arg-type]
    calls: list[str] = []
    commit_started = threading.Event()
    release_commit = threading.Event()

    def commit_sync() -> None:
        calls.append("commit")
        commit_started.set()
        assert release_commit.wait(timeout=2)

    def rollback_sync() -> None:
        calls.append("rollback")

    transaction._commit_sync = commit_sync  # type: ignore[method-assign]
    transaction._rollback_sync = rollback_sync  # type: ignore[method-assign]

    commit_task = asyncio.create_task(transaction.commit())
    assert await asyncio.to_thread(commit_started.wait, 2)

    rollback_task = asyncio.create_task(transaction.rollback())
    await asyncio.sleep(0)
    release_commit.set()

    await asyncio.gather(commit_task, rollback_task)

    assert calls == ["commit"]
    assert store.end_calls == 1


def test_open_tx_connection_closes_connection_if_begin_fails(tmp_path: Path):
    class FailingConnection:
        def __init__(self):
            self.closed = False

        def execute(self, statement: str) -> None:
            assert statement == "BEGIN IMMEDIATE"
            raise RuntimeError("begin failed")

        def close(self) -> None:
            self.closed = True

    store = make_store(tmp_path)
    connection = FailingConnection()
    store._connect = lambda: connection  # type: ignore[method-assign,return-value]

    with pytest.raises(RuntimeError, match="begin failed"):
        store._open_tx_connection()

    assert connection.closed
