"""Per-execution metadata on the file store: durable across reopen, preserved
across complete, and removed when the execution is deleted."""

from pathlib import Path

import pytest
from glyff import ExecutionId
from glyff.serialization import JsonSerializer

from glyff_file_store import JsonFileSessionStore
from glyff_file_store._file_client import FileClient


def _eid(name: str) -> ExecutionId:
    return ExecutionId(parent_id=None, name=name, sequence=0, args_hash="h")


def _reopen(
    tmp_path: Path, sid: str, serializer: JsonSerializer
) -> JsonFileSessionStore:
    return JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id=sid), serializer=serializer
    )


async def test_metadata_survives_reopen_and_complete(
    store_factory, tmp_path, serializer
):
    store: JsonFileSessionStore = store_factory("meta")
    eid = _eid("root")

    tx = await store.begin_transaction()
    execution = await store.start_execution(eid)
    await store.set_metadata(eid, "trace", {"step": 1}, dict)
    await execution.complete("result", str)
    await tx.commit()

    reopened = _reopen(tmp_path, "meta", serializer)
    record = await reopened.get_execution_record(eid, str)
    assert record is not None and record.result == "result"
    assert await reopened.get_metadata(eid, "trace", dict) == {"step": 1}
    assert await reopened.get_metadata(eid, "absent", dict) is None


async def test_metadata_rolls_back(store_factory):
    store: JsonFileSessionStore = store_factory("meta-rb")
    eid = _eid("root")
    tx = await store.begin_transaction()
    await store.start_execution(eid)
    await tx.commit()

    tx = await store.begin_transaction()
    await store.set_metadata(eid, "k", "v", str)
    await tx.rollback()

    assert await store.get_metadata(eid, "k", str) is None


async def test_delete_removes_metadata(store_factory):
    store: JsonFileSessionStore = store_factory("meta-del")
    eid = _eid("root")
    tx = await store.begin_transaction()
    await store.start_execution(eid)
    await store.set_metadata(eid, "k", "v", str)
    await tx.commit()

    tx = await store.begin_transaction()
    await store.repository.delete_executions([eid])
    await tx.commit()

    assert await store.get_metadata(eid, "k", str) is None


async def test_set_metadata_unknown_execution_raises(store_factory):
    store: JsonFileSessionStore = store_factory("meta-unknown")
    tx = await store.begin_transaction()
    await store.set_metadata(_eid("ghost"), "k", "v", str)
    with pytest.raises(LookupError):
        await tx.commit()


async def test_get_metadata_unknown_execution_returns_none(store_factory):
    store: JsonFileSessionStore = store_factory("meta-none")
    assert await store.get_metadata(_eid("ghost"), "k", str) is None
