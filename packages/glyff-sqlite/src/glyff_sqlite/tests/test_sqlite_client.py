import json
from pathlib import Path

from glyff import Execution, ExecutionId, SerializedValue, SessionId, TransactionScope
from glyff.store.utils import execution_id_to_path
from glyff.testing import canonical_arguments, make_execution_id
from glyff_sqlite import SQLiteBackend
from glyff.store.staging import (
    DeleteExecution,
    ExecutionKey,
    ExecutionSnapshot,
    SaveExecution,
)

from glyff_sqlite._sqlite_client import SQLiteClient

SESSION = SessionId("test")


def _save(execution_id: ExecutionId) -> SaveExecution:
    execution = Execution.start(execution_id, canonical_arguments())
    return SaveExecution(ExecutionSnapshot.from_execution(execution))


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
    assert [row[1] for row in rows] == [
        "session_id",
        "path",
        "arguments",
        "status",
        "result",
        "metadata",
    ]


async def test_sqlite_client_commits_a_batch_of_mutations(tmp_path: Path):
    client = SQLiteClient(tmp_path / "atomic.sqlite3")
    client.initialize_schema_sync()
    root = make_execution_id("task")
    child = make_execution_id("child", parent=root)

    await client.commit_mutations(
        {
            ExecutionKey(SESSION, root): _save(root),
            ExecutionKey(SESSION, child): _save(child),
        }
    )

    for execution_id in (root, child):
        path = execution_id_to_path(execution_id)
        assert await client.read_committed(SESSION.value, path) is not None


async def test_sqlite_client_commits_a_delete(tmp_path: Path):
    client = SQLiteClient(tmp_path / "delete.sqlite3")
    client.initialize_schema_sync()
    execution_id = make_execution_id("task")
    path = execution_id_to_path(execution_id)

    await client.commit_mutations(
        {ExecutionKey(SESSION, execution_id): _save(execution_id)}
    )
    await client.commit_mutations(
        {ExecutionKey(SESSION, execution_id): DeleteExecution()}
    )

    assert await client.read_committed(SESSION.value, path) is None


async def test_sqlite_backend_stores_execution_columns_as_readable_json(
    tmp_path: Path,
):
    db = tmp_path / "readable.sqlite3"
    backend = SQLiteBackend(db)
    execution_id = make_execution_id("task")
    execution = Execution.start(execution_id, canonical_arguments())
    execution.complete(SerializedValue(b'{"answer":42}'))
    execution.set_metadata("trace", SerializedValue(b'{"step":1}'))

    async with TransactionScope(backend.transaction_provider):
        await backend.repository.save(SESSION, execution)

    client = SQLiteClient(db)
    rows = await client.read_sql(
        "SELECT status, result, metadata FROM glyff_executions WHERE path = ?",
        execution_id_to_path(execution_id),
    )

    assert len(rows) == 1
    status, result, metadata = rows[0]
    assert status == "completed"
    assert json.loads(result) == {"answer": 42}
    assert json.loads(metadata) == {"trace": {"step": 1}}
