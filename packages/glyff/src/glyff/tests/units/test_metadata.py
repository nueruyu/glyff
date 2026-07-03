"""Per-execution metadata is owned by the Execution aggregate."""

import pytest

from glyff import (
    ArgsHasher,
    Execution,
    ExecutionId,
    SerializedValue,
    Session,
    engrave,
    get_context,
)
from glyff.exceptions import NoCurrentExecutionError
from glyff.store import MemorySessionStore
from glyff.store._memory_client import MemoryClient
from glyff.tests.types import StoreFactory


def _store(serializer) -> MemorySessionStore:
    return MemorySessionStore(client=MemoryClient(), serializer=serializer)


def _eid(name: str, parent: ExecutionId | None = None) -> ExecutionId:
    return ExecutionId(parent_id=parent, name=name, sequence=0, args_hash="h")


async def _start(store: MemorySessionStore, eid: ExecutionId) -> None:
    tx = await store.begin_transaction()
    await store.save(Execution.start(eid))
    await tx.commit()


async def _set_metadata(store, serializer, eid, key, value, value_type) -> None:
    execution = await store.get(eid)
    if execution is None:
        raise LookupError(f"Execution {eid} not found")
    execution.set_metadata(
        key,
        SerializedValue(await serializer.serialize(value, value_type)),
    )
    await store.save(execution)


async def _get_metadata(store, serializer, eid, key, return_type):
    execution = await store.get(eid)
    if execution is None:
        return None
    metadata = execution.get_metadata(key)
    if metadata is None:
        return None
    return await serializer.deserialize(metadata.value.data, return_type)


async def test_set_get_roundtrip(serializer):
    store = _store(serializer)
    eid = _eid("root")
    await _start(store, eid)

    tx = await store.begin_transaction()
    await _set_metadata(store, serializer, eid, "note", {"a": 1}, dict)
    await tx.commit()

    assert await _get_metadata(store, serializer, eid, "note", dict) == {"a": 1}
    assert await _get_metadata(store, serializer, eid, "missing", dict) is None


async def test_keyed_entries_are_independent(serializer):
    store = _store(serializer)
    eid = _eid("root")
    await _start(store, eid)

    tx = await store.begin_transaction()
    await _set_metadata(store, serializer, eid, "a", "one", str)
    await _set_metadata(store, serializer, eid, "b", "two", str)
    await tx.commit()

    tx = await store.begin_transaction()
    await _set_metadata(store, serializer, eid, "a", "ONE", str)
    await tx.commit()

    assert await _get_metadata(store, serializer, eid, "a", str) == "ONE"
    assert await _get_metadata(store, serializer, eid, "b", str) == "two"


async def test_metadata_is_co_transactional(serializer):
    store = _store(serializer)
    eid = _eid("root")
    await _start(store, eid)

    tx = await store.begin_transaction()
    await _set_metadata(store, serializer, eid, "note", "staged", str)
    await tx.rollback()

    assert await _get_metadata(store, serializer, eid, "note", str) is None


async def test_complete_preserves_metadata(serializer):
    store = _store(serializer)
    eid = _eid("root")

    tx = await store.begin_transaction()
    execution = Execution.start(eid)
    execution.set_metadata("note", SerializedValue(await serializer.serialize("kept", str)))
    execution.complete(SerializedValue(await serializer.serialize("result", str)))
    await store.save(execution)
    await tx.commit()

    record = await store.get(eid)
    assert record is not None and record.result is not None
    assert await serializer.deserialize(record.result.data, str) == "result"
    assert await _get_metadata(store, serializer, eid, "note", str) == "kept"


async def test_delete_many_removes_metadata(serializer):
    store = _store(serializer)
    eid = _eid("root")
    await _start(store, eid)
    tx = await store.begin_transaction()
    await _set_metadata(store, serializer, eid, "note", "gone", str)
    await tx.commit()

    tx = await store.begin_transaction()
    await store.delete_many([eid])
    await tx.commit()

    assert await _get_metadata(store, serializer, eid, "note", str) is None


async def test_set_metadata_unknown_execution_raises(serializer):
    store = _store(serializer)
    tx = await store.begin_transaction()
    with pytest.raises(LookupError):
        await _set_metadata(store, serializer, _eid("ghost"), "k", "v", str)
    await tx.rollback()


async def test_get_metadata_unknown_execution_returns_none(serializer):
    store = _store(serializer)
    assert await _get_metadata(store, serializer, _eid("ghost"), "k", str) is None


async def test_ctx_metadata_roundtrips_and_persists(
    store_factory: StoreFactory, hasher: ArgsHasher, serializer
):
    captured: dict[str, ExecutionId] = {}

    @engrave
    async def annotate() -> str:
        ctx = get_context()
        await ctx.set_metadata("trace", {"step": 1})
        assert await ctx.get_metadata("trace", dict) == {"step": 1}
        captured["id"] = ctx.current_execution_id  # type: ignore[assignment]
        return "done"

    store = store_factory("meta-ctx")
    async with Session(id="meta-ctx", store=store, serializer=serializer, hasher=hasher):
        result = await annotate()

    assert result == "done"
    assert await _get_metadata(store, serializer, captured["id"], "trace", dict) == {
        "step": 1
    }


async def test_ctx_set_metadata_requires_active_execution():
    from glyff._context import Context, reset_context, set_context
    from glyff._event_system import EventEmitter
    from glyff._sequencer import Sequencer
    from glyff.serialization import JsonArgsHasher, JsonSerializer

    store = _store(JsonSerializer())
    ctx = Context(
        session_id="no-exec",
        executions=store,
        transactions=store,
        serializer=JsonSerializer(),
        sequencer=Sequencer(),
        hasher=JsonArgsHasher(),
        event_emitter=EventEmitter([]),
    )
    token = set_context(ctx)
    try:
        with pytest.raises(NoCurrentExecutionError):
            await ctx.set_metadata("k", "v")
    finally:
        reset_context(token)
