import json
from pathlib import Path

from glyff import Execution, ExecutionId, SerializedValue, TransactionScope
from glyff_sqlite import SQLiteBackend
from glyff_sqlite._sqlite_client import SQLiteClient, SQLiteExecutionRecord


def record(value: str) -> SQLiteExecutionRecord:
    return SQLiteExecutionRecord(
        status="completed",
        result=f'"{value}"',
        metadata="{}",
    )


async def test_sqlite_backend_initializes_schema(tmp_path: Path):
    db = tmp_path / "schema.sqlite3"

    SQLiteBackend(db)
    client = SQLiteClient(db)
    rows = await client.read_sql(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = 'glyff_executions'"
    )

    assert rows == [("glyff_executions",)]


async def test_sqlite_backend_reopens_existing_database(tmp_path: Path):
    db = tmp_path / "existing.sqlite3"

    SQLiteBackend(db)
    SQLiteBackend(db)

    client = SQLiteClient(db)
    rows = await client.read_sql("PRAGMA table_info(glyff_executions)")
    assert [row[1] for row in rows] == ["path", "status", "result", "metadata"]


async def test_sqlite_client_commit_is_atomic_across_execution_paths(tmp_path: Path):
    client = SQLiteClient(tmp_path / "atomic.sqlite3")
    client._initialize_schema_sync()

    token, _ = client.begin_staging()
    client.stage_write("task", record("execution"))
    client.stage_write("task/child", record("child"))
    await client.commit_staged()
    client.end_staging(token)

    assert await client.read("task") == record("execution")
    assert await client.read("task/child") == record("child")


async def test_sqlite_client_rollback_clears_all_execution_paths(tmp_path: Path):
    client = SQLiteClient(tmp_path / "rollback.sqlite3")
    client._initialize_schema_sync()

    token, _ = client.begin_staging()
    client.stage_write("task", record("execution"))
    client.stage_write("task/child", record("child"))
    await client.clear_staged()
    client.end_staging(token)

    assert await client.read("task") is None
    assert await client.read("task/child") is None


async def test_sqlite_backend_stores_execution_columns_as_readable_json(
    tmp_path: Path,
):
    db = tmp_path / "readable.sqlite3"
    backend = SQLiteBackend(db)
    execution_id = ExecutionId(
        parent_id=None,
        name="task",
        sequence=0,
        args_hash="hash",
    )
    execution = Execution.start(execution_id)
    execution.complete(SerializedValue(b'{"answer":42}'))
    execution.set_metadata("trace", SerializedValue(b'{"step":1}'))

    async with TransactionScope(backend.transaction_provider):
        await backend.repository.save(execution)

    client = SQLiteClient(db)
    rows = await client.read_sql(
        "SELECT status, result, metadata FROM glyff_executions WHERE path = ?",
        "task#0:hash",
    )

    assert len(rows) == 1
    status, result, metadata = rows[0]
    assert status == "completed"
    assert json.loads(result) == {"answer": 42}
    assert json.loads(metadata) == {"trace": {"step": 1}}
