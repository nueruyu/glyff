from pathlib import Path

from glyff_sqlite import SQLiteBackend
from glyff_sqlite._sqlite_client import SQLiteClient


async def test_sqlite_backend_initializes_schema(tmp_path: Path):
    db = tmp_path / "schema.sqlite3"

    SQLiteBackend(db)
    client = SQLiteClient(db)
    rows = await client.read_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'records'"
    )

    assert rows == [("records",)]


async def test_sqlite_backend_reopens_existing_database(tmp_path: Path):
    db = tmp_path / "existing.sqlite3"

    SQLiteBackend(db)
    SQLiteBackend(db)

    client = SQLiteClient(db)
    rows = await client.read_sql("PRAGMA table_info(records)")
    assert [row[1] for row in rows] == ["namespace", "key", "value"]


async def test_sqlite_client_commit_is_atomic_across_namespaces(tmp_path: Path):
    client = SQLiteClient(tmp_path / "atomic.sqlite3")
    client._initialize_schema_sync()

    token, _ = client.begin_staging()
    client.stage_write("executions", "task", b"execution")
    client.stage_write("metadata", "task", b"metadata")
    await client.commit_staged()
    client.end_staging(token)

    assert await client.read("executions", "task") == b"execution"
    assert await client.read("metadata", "task") == b"metadata"


async def test_sqlite_client_rollback_clears_all_namespaces(tmp_path: Path):
    client = SQLiteClient(tmp_path / "rollback.sqlite3")
    client._initialize_schema_sync()

    token, _ = client.begin_staging()
    client.stage_write("executions", "task", b"execution")
    client.stage_write("metadata", "task", b"metadata")
    await client.clear_staged()
    client.end_staging(token)

    assert await client.read("executions", "task") is None
    assert await client.read("metadata", "task") is None
