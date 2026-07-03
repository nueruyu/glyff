"""Per-execution metadata on the SQLite store: durable across reopen, preserved
across complete, and removed when the execution is deleted."""

from pathlib import Path

from glyff import ExecutionId
from glyff.serialization import JsonSerializer

from glyff_sqlite import SQLiteSessionStore


def _eid(name: str) -> ExecutionId:
    return ExecutionId(parent_id=None, name=name, sequence=0, args_hash="h")


async def test_metadata_survives_reopen_and_complete(tmp_path: Path):
    serializer = JsonSerializer()
    db = tmp_path / "meta.sqlite3"
    store = SQLiteSessionStore(db, serializer)
    eid = _eid("root")

    tx = await store.begin_transaction()
    execution = await store.start_execution(eid)
    await store.set_metadata(eid, "trace", {"step": 1}, dict)
    await execution.complete("result", str)
    await tx.commit()

    reopened = SQLiteSessionStore(db, serializer)
    record = await reopened.get_execution_record(eid, str)
    assert record is not None and record.result == "result"
    assert await reopened.get_metadata(eid, "trace", dict) == {"step": 1}
    assert await reopened.get_metadata(eid, "absent", dict) is None


async def test_metadata_rolls_back(tmp_path: Path):
    store = SQLiteSessionStore(tmp_path / "meta-rb.sqlite3", JsonSerializer())
    eid = _eid("root")
    tx = await store.begin_transaction()
    await store.start_execution(eid)
    await tx.commit()

    tx = await store.begin_transaction()
    await store.set_metadata(eid, "k", "v", str)
    await tx.rollback()

    assert await store.get_metadata(eid, "k", str) is None


async def test_delete_removes_metadata(tmp_path: Path):
    store = SQLiteSessionStore(tmp_path / "meta-del.sqlite3", JsonSerializer())
    eid = _eid("root")
    tx = await store.begin_transaction()
    await store.start_execution(eid)
    await store.set_metadata(eid, "k", "v", str)
    await tx.commit()

    tx = await store.begin_transaction()
    await store.repository.delete_executions([eid])
    await tx.commit()

    assert await store.get_metadata(eid, "k", str) is None
