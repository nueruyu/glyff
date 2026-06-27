import asyncio
from pathlib import Path

import pytest
from glyff import ExecutionId, ExecutionStatus
from glyff.serialization import JsonSerializer
from glyff.store.utils import execution_id_to_path

from glyff_file_store import SQLiteClient, SQLiteSessionStore


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


# -- Client does not create domain tables ------------------------------------


async def test_sqlite_client_does_not_create_domain_tables(tmp_path):
    client = SQLiteClient(tmp_path / "session.sqlite3")

    tables = await client.read(
        lambda c: {
            row[0]
            for row in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    )

    assert "executions" not in tables
    assert "blobs" not in tables


async def test_sqlite_session_store_creates_executions_table(tmp_path, serializer):
    client = SQLiteClient(tmp_path / "session.sqlite3")
    SQLiteSessionStore(client=client, serializer=serializer)

    tables = await client.read(
        lambda c: {
            row[0]
            for row in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    )

    assert "executions" in tables
    assert "blobs" not in tables


# -- Basic store operations --------------------------------------------------


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


# -- Descendants -------------------------------------------------------------


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


# -- Parallel completions ----------------------------------------------------


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


# -- Atomic commit / rollback ------------------------------------------------


async def test_multiple_writes_in_one_transaction_commit_atomically(tmp_path: Path):
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


# -- Nested transactions -----------------------------------------------------


async def test_sqlite_nested_child_commit_is_independent_of_parent_staging(
    tmp_path: Path,
):
    parent = make_execution_id("parent")
    child = make_execution_id("child", parent_id=parent)
    store = make_store(tmp_path)

    parent_tx = await store.begin_transaction()
    await store.start_execution(parent)

    child_tx = await store.begin_transaction()
    child_execution = await store.start_execution(child)
    await child_execution.complete("child", str)
    await child_tx.commit()

    parent_record = await store.get_execution_record(parent, str)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    child_record = await store.get_execution_record(child, str)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    await parent_tx.rollback()

    assert await store.get_execution_record(parent, str) is None
    child_record = await store.get_execution_record(child, str)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED


async def test_sqlite_parent_staging_survives_nested_child_rollback(tmp_path: Path):
    parent = make_execution_id("parent")
    child = make_execution_id("child", parent_id=parent)
    store = make_store(tmp_path)

    parent_tx = await store.begin_transaction()
    await store.start_execution(parent)

    child_tx = await store.begin_transaction()
    child_execution = await store.start_execution(child)
    await child_execution.complete("child", str)
    await child_tx.rollback()

    parent_record = await store.get_execution_record(parent, str)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    assert await store.get_execution_record(child, str) is None

    await parent_tx.commit()

    parent_record = await store.get_execution_record(parent, str)
    assert parent_record is not None
    assert parent_record.status == ExecutionStatus.STARTED
    assert await store.get_execution_record(child, str) is None


# -- Execution and external metadata atomicity --------------------------------


async def test_sqlite_execution_and_external_metadata_commit_together(
    tmp_path,
    serializer,
):
    client = SQLiteClient(tmp_path / "session.sqlite3")
    store = SQLiteSessionStore(client=client, serializer=serializer)

    client._apply_sync(
        lambda c: c.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL
            )
            """
        )
    )

    execution_id = ExecutionId(
        parent_id=None,
        name="root",
        sequence=0,
        args_hash="root",
    )

    tx = await store.begin_transaction()
    execution = await store.start_execution(execution_id)
    await execution.complete("ok", str)

    client.stage(
        lambda c: c.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("root", b"metadata"),
        )
    )

    await tx.commit()

    record = await store.get_execution_record(execution_id, str)
    assert record is not None
    assert record.status == ExecutionStatus.COMPLETED
    assert record.result == "ok"

    row = await client.read(
        lambda c: c.execute(
            "SELECT value FROM metadata WHERE key = ?",
            ("root",),
        ).fetchone()
    )
    assert row is not None
    assert row[0] == b"metadata"


async def test_sqlite_execution_and_external_metadata_rollback_together(
    tmp_path,
    serializer,
):
    client = SQLiteClient(tmp_path / "session.sqlite3")
    store = SQLiteSessionStore(client=client, serializer=serializer)

    client._apply_sync(
        lambda c: c.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL
            )
            """
        )
    )

    execution_id = ExecutionId(
        parent_id=None,
        name="root",
        sequence=0,
        args_hash="root",
    )

    tx = await store.begin_transaction()
    execution = await store.start_execution(execution_id)
    await execution.complete("ok", str)

    client.stage(
        lambda c: c.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("root", b"metadata"),
        )
    )

    await tx.rollback()

    assert await store.get_execution_record(execution_id, str) is None

    row = await client.read(
        lambda c: c.execute(
            "SELECT value FROM metadata WHERE key = ?",
            ("root",),
        ).fetchone()
    )
    assert row is None


async def test_sqlite_child_commit_does_not_commit_parent_metadata(
    tmp_path,
    serializer,
):
    client = SQLiteClient(tmp_path / "session.sqlite3")
    store = SQLiteSessionStore(client=client, serializer=serializer)

    client._apply_sync(
        lambda c: c.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL
            )
            """
        )
    )

    parent = ExecutionId(parent_id=None, name="parent", sequence=0, args_hash="p")
    child = ExecutionId(parent_id=parent, name="child", sequence=0, args_hash="c")

    parent_tx = await store.begin_transaction()
    await store.start_execution(parent)

    client.stage(
        lambda c: c.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("parent", b"parent"),
        )
    )

    child_tx = await store.begin_transaction()
    child_execution = await store.start_execution(child)
    await child_execution.complete("child", str)
    client.stage(
        lambda c: c.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("child", b"child"),
        )
    )
    await child_tx.commit()

    child_record = await store.get_execution_record(child, str)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    def read_metadata(key: str):
        return lambda c: c.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()

    assert await client.read(read_metadata("child")) is not None
    assert await client.read(read_metadata("parent")) is None

    await parent_tx.rollback()

    assert await client.read(read_metadata("child")) is not None
    assert await client.read(read_metadata("parent")) is None


# -- Concurrent close --------------------------------------------------------


async def test_sqlite_transaction_close_is_idempotent(tmp_path: Path):
    store = make_store(tmp_path)
    transaction = await store.begin_transaction()
    await transaction.commit()
    await transaction.commit()
    await transaction.rollback()
    assert True


async def test_sqlite_begin_transaction_does_not_open_physical_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = make_store(tmp_path)

    def fail_connect():
        raise AssertionError("begin_transaction should not connect")

    monkeypatch.setattr(store._client, "_connect", fail_connect)
    transaction = await store.begin_transaction()
    await transaction.rollback()


# -- Constructor variants ----------------------------------------------------


async def test_sqlite_store_constructed_with_client(serializer):
    client = SQLiteClient(":memory:")
    store = SQLiteSessionStore(client=client, serializer=serializer)
    assert store.client is client


async def test_sqlite_store_raises_on_both_path_and_client(serializer):
    with pytest.raises(TypeError, match="not both"):
        SQLiteSessionStore(":memory:", serializer, client=SQLiteClient(":memory:"))


async def test_sqlite_store_raises_on_no_path_or_client(serializer):
    with pytest.raises(TypeError, match="required"):
        SQLiteSessionStore(serializer=serializer)


async def test_sqlite_store_raises_on_no_serializer(tmp_path):
    with pytest.raises(TypeError, match="serializer"):
        SQLiteSessionStore(tmp_path / "e.sqlite3")


# -- Out-of-order close ------------------------------------------------------


# -- Three-level nesting -----------------------------------------------------


async def test_sqlite_three_level_nested_transactions(tmp_path):
    root = make_execution_id("root")
    child = make_execution_id("child", parent_id=root)
    grandchild = make_execution_id("grandchild", parent_id=child)
    store = make_store(tmp_path)

    root_tx = await store.begin_transaction()
    await store.start_execution(root)

    child_tx = await store.begin_transaction()
    child_execution = await store.start_execution(child)

    grandchild_tx = await store.begin_transaction()
    grandchild_execution = await store.start_execution(grandchild)
    await grandchild_execution.complete("grandchild", str)
    await grandchild_tx.commit()

    await child_execution.complete("child", str)
    await child_tx.commit()

    await root_tx.rollback()

    assert await store.get_execution_record(root, str) is None

    child_record = await store.get_execution_record(child, str)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    grandchild_record = await store.get_execution_record(grandchild, str)
    assert grandchild_record is not None
    assert grandchild_record.status == ExecutionStatus.COMPLETED
