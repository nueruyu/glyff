"""Per-execution metadata: typed, keyed, co-transactional, and scoped to the
execution's lifetime (deleted with it, preserved across complete/fail)."""

import pytest

from glyff import ArgsHasher, ExecutionId, Session, engrave, get_context
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
    await store.start_execution(eid)
    await tx.commit()


# -- Repository mechanism ----------------------------------------------------


async def test_set_get_roundtrip(serializer):
    store = _store(serializer)
    eid = _eid("root")
    await _start(store, eid)

    tx = await store.begin_transaction()
    await store.set_metadata(eid, "note", {"a": 1}, dict)
    await tx.commit()

    assert await store.get_metadata(eid, "note", dict) == {"a": 1}
    assert await store.get_metadata(eid, "missing", dict) is None


async def test_keyed_entries_are_independent(serializer):
    store = _store(serializer)
    eid = _eid("root")
    await _start(store, eid)

    tx = await store.begin_transaction()
    await store.set_metadata(eid, "a", "one", str)
    await store.set_metadata(eid, "b", "two", str)
    await tx.commit()

    # Overwrite one key; the other survives.
    tx = await store.begin_transaction()
    await store.set_metadata(eid, "a", "ONE", str)
    await tx.commit()

    assert await store.get_metadata(eid, "a", str) == "ONE"
    assert await store.get_metadata(eid, "b", str) == "two"


async def test_metadata_is_co_transactional(serializer):
    store = _store(serializer)
    eid = _eid("root")
    await _start(store, eid)

    tx = await store.begin_transaction()
    await store.set_metadata(eid, "note", "staged", str)
    await tx.rollback()

    assert await store.get_metadata(eid, "note", str) is None


async def test_complete_preserves_metadata(serializer):
    store = _store(serializer)
    eid = _eid("root")

    tx = await store.begin_transaction()
    execution = await store.start_execution(eid)
    await store.set_metadata(eid, "note", "kept", str)
    await execution.complete("result", str)
    await tx.commit()

    record = await store.get_execution_record(eid, str)
    assert record is not None and record.result == "result"
    assert await store.get_metadata(eid, "note", str) == "kept"


async def test_delete_executions_removes_metadata(serializer):
    store = _store(serializer)
    eid = _eid("root")
    await _start(store, eid)
    tx = await store.begin_transaction()
    await store.set_metadata(eid, "note", "gone", str)
    await tx.commit()

    tx = await store.begin_transaction()
    await store.repository.delete_executions([eid])
    await tx.commit()

    assert await store.get_metadata(eid, "note", str) is None


# -- ctx API through a live session ------------------------------------------


async def test_ctx_metadata_roundtrips_and_persists(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    captured: dict[str, ExecutionId] = {}

    @engrave
    async def annotate() -> str:
        ctx = get_context()
        await ctx.set_metadata("trace", {"step": 1})
        # Read-your-writes within the running execution.
        assert await ctx.get_metadata("trace", dict) == {"step": 1}
        captured["id"] = ctx.current_execution_id  # type: ignore[assignment]
        return "done"

    store = store_factory("meta-ctx")
    async with Session(id="meta-ctx", store=store, hasher=hasher):
        result = await annotate()

    assert result == "done"
    # Written to the current execution and committed with the body scope.
    assert await store.get_metadata(captured["id"], "trace", dict) == {"step": 1}


async def test_ctx_set_metadata_requires_active_execution():
    @engrave
    async def _noop() -> None:
        return None

    from glyff._context import Context, set_context
    from glyff._event_system import EventEmitter
    from glyff._sequencer import Sequencer
    from glyff.serialization import JsonArgsHasher, JsonSerializer

    ctx = Context(
        session_id="no-exec",
        store=_store(JsonSerializer()),
        sequencer=Sequencer(),
        hasher=JsonArgsHasher(),
        event_emitter=EventEmitter([]),
    )
    token = set_context(ctx)
    try:
        with pytest.raises(NoCurrentExecutionError):
            await ctx.set_metadata("k", "v")
    finally:
        from glyff._context import reset_context

        reset_context(token)
