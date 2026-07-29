"""Per-execution metadata is accessed through MetadataAccessor."""

import pytest

from glyff import (
    ArgsCanonicalizer,
    Execution,
    ExecutionId,
    MetadataAccessor,
    SerializedValue,
    engrave,
    get_context,
)
from glyff._context import Context, reset_context, set_context
from glyff._event_system import EventEmitter
from glyff.testing import canonical_args, eid
from glyff._sequencer import Sequencer
from glyff.exceptions import NoCurrentExecutionError
from glyff.serialization import JsonArgsCanonicalizer, JsonSerializer
from glyff.store import MemoryBackend
from glyff.tests.types import BackendFactory, make_session


def _eid(name: str, parent: ExecutionId | None = None) -> ExecutionId:
    return eid(name, parent=parent)


async def _start(ctx: Context, eid: ExecutionId) -> None:
    async with ctx.get_transaction_scope():
        await ctx.repository.save(Execution.start(eid, canonical_args()))


async def test_set_get_roundtrip(test_context: Context):
    accessor = MetadataAccessor(test_context)
    eid = _eid("root")

    async with test_context.get_transaction_scope():
        await test_context.repository.save(Execution.start(eid, canonical_args()))
        test_context.tracer.start(eid)
        try:
            await accessor.set("note", {"a": 1})
        finally:
            test_context.tracer.end()

    assert await accessor.get("note", dict, execution_id=eid) == {"a": 1}
    assert await accessor.get("missing", dict, execution_id=eid) is None


async def test_keyed_entries_are_independent(test_context: Context):
    accessor = MetadataAccessor(test_context)
    eid = _eid("root")

    async with test_context.get_transaction_scope():
        await test_context.repository.save(Execution.start(eid, canonical_args()))
        test_context.tracer.start(eid)
        try:
            await accessor.set("a", "one")
            await accessor.set("b", "two")
        finally:
            test_context.tracer.end()

    async with test_context.get_transaction_scope():
        test_context.tracer.start(eid)
        try:
            await accessor.set("a", "ONE")
        finally:
            test_context.tracer.end()

    assert await accessor.get("a", str, execution_id=eid) == "ONE"
    assert await accessor.get("b", str, execution_id=eid) == "two"


async def test_metadata_is_co_transactional(test_context: Context):
    accessor = MetadataAccessor(test_context)
    eid = _eid("root")
    await _start(test_context, eid)

    scope = test_context.get_transaction_scope()
    await scope.__aenter__()
    test_context.tracer.start(eid)
    try:
        await accessor.set("note", "staged")
    finally:
        test_context.tracer.end()
    await scope.rollback()

    assert await accessor.get("note", str, execution_id=eid) is None


async def test_complete_preserves_metadata(test_context: Context, serializer):
    accessor = MetadataAccessor(test_context)
    eid = _eid("root")

    async with test_context.get_transaction_scope():
        await test_context.repository.save(Execution.start(eid, canonical_args()))
        test_context.tracer.start(eid)
        try:
            await accessor.set("note", "kept")
        finally:
            test_context.tracer.end()

        execution = await test_context.repository.get(eid)
        assert execution is not None
        execution.complete(SerializedValue(await serializer.serialize("result", str)))
        await test_context.repository.save(execution)

    record = await test_context.repository.get(eid)
    assert record is not None and record.result is not None
    assert await serializer.deserialize(record.result.data, str) == "result"
    assert await accessor.get("note", str, execution_id=eid) == "kept"


async def test_delete_many_removes_metadata(test_context: Context):
    accessor = MetadataAccessor(test_context)
    eid = _eid("root")
    await _start(test_context, eid)

    async with test_context.get_transaction_scope():
        test_context.tracer.start(eid)
        try:
            await accessor.set("note", "gone")
        finally:
            test_context.tracer.end()

    async with test_context.get_transaction_scope():
        await test_context.repository.delete_many([eid])

    assert await accessor.get("note", str, execution_id=eid) is None


async def test_set_metadata_unknown_execution_raises(test_context: Context):
    accessor = MetadataAccessor(test_context)
    scope = test_context.get_transaction_scope()
    await scope.__aenter__()
    test_context.tracer.start(_eid("ghost"))
    try:
        with pytest.raises(LookupError):
            await accessor.set("k", "v")
    finally:
        test_context.tracer.end()
        await scope.rollback()


async def test_get_metadata_unknown_execution_returns_none(test_context: Context):
    accessor = MetadataAccessor(test_context)
    assert await accessor.get("k", str, execution_id=_eid("ghost")) is None


async def test_ctx_metadata_roundtrips_and_persists(
    backend_factory: BackendFactory, canonicalizer: ArgsCanonicalizer, serializer
):
    captured: dict[str, ExecutionId] = {}

    @engrave
    async def annotate() -> str:
        ctx = get_context()
        await ctx.metadata.set("trace", {"step": 1})
        assert await ctx.metadata.get("trace", dict) == {"step": 1}
        captured["id"] = ctx.current_execution_id  # type: ignore[assignment]
        return "done"

    backend = backend_factory("meta-ctx")
    async with make_session("meta-ctx", backend, canonicalizer, serializer):
        result = await annotate()

    assert result == "done"

    loaded = await backend.repository.get(captured["id"])
    assert loaded is not None
    meta = loaded.get_metadata("trace")
    assert meta is not None
    assert await serializer.deserialize(meta.value.data, dict) == {"step": 1}


async def test_ctx_set_metadata_requires_active_execution():
    backend = MemoryBackend()
    ctx = Context(
        session_id="no-exec",
        backend=backend,
        serializer=JsonSerializer(),
        sequencer=Sequencer(),
        canonicalizer=JsonArgsCanonicalizer(),
        event_emitter=EventEmitter([]),
    )
    token = set_context(ctx)
    try:
        with pytest.raises(NoCurrentExecutionError):
            await ctx.metadata.set("k", "v")
    finally:
        reset_context(token)
