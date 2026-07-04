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
from glyff._context import TransactionScope
from glyff.exceptions import NoCurrentExecutionError
from glyff.store import MemoryBackend
from glyff.tests.types import BackendFactory


def _eid(name: str, parent: ExecutionId | None = None) -> ExecutionId:
    return ExecutionId(parent_id=parent, name=name, sequence=0, args_hash="h")


async def _start(backend: MemoryBackend, eid: ExecutionId) -> None:
    async with TransactionScope(backend.transaction_provider):
        await backend.repository.save(Execution.start(eid))


async def _set_metadata(backend, serializer, eid, key, value, value_type) -> None:
    execution = await backend.repository.get(eid)
    if execution is None:
        raise LookupError(f"Execution {eid} not found")
    execution.set_metadata(
        key,
        SerializedValue(await serializer.serialize(value, value_type)),
    )
    await backend.repository.save(execution)


async def _get_metadata(backend, serializer, eid, key, return_type):
    execution = await backend.repository.get(eid)
    if execution is None:
        return None
    metadata = execution.get_metadata(key)
    if metadata is None:
        return None
    return await serializer.deserialize(metadata.value.data, return_type)


async def test_set_get_roundtrip(serializer):
    backend = MemoryBackend()
    eid = _eid("root")
    await _start(backend, eid)

    async with TransactionScope(backend.transaction_provider):
        await _set_metadata(backend, serializer, eid, "note", {"a": 1}, dict)

    assert await _get_metadata(backend, serializer, eid, "note", dict) == {"a": 1}
    assert await _get_metadata(backend, serializer, eid, "missing", dict) is None


async def test_keyed_entries_are_independent(serializer):
    backend = MemoryBackend()
    eid = _eid("root")
    await _start(backend, eid)

    async with TransactionScope(backend.transaction_provider):
        await _set_metadata(backend, serializer, eid, "a", "one", str)
        await _set_metadata(backend, serializer, eid, "b", "two", str)

    async with TransactionScope(backend.transaction_provider):
        await _set_metadata(backend, serializer, eid, "a", "ONE", str)

    assert await _get_metadata(backend, serializer, eid, "a", str) == "ONE"
    assert await _get_metadata(backend, serializer, eid, "b", str) == "two"


async def test_metadata_is_co_transactional(serializer):
    backend = MemoryBackend()
    eid = _eid("root")
    await _start(backend, eid)

    scope = TransactionScope(backend.transaction_provider)
    await scope.__aenter__()
    await _set_metadata(backend, serializer, eid, "note", "staged", str)
    await scope.rollback()

    assert await _get_metadata(backend, serializer, eid, "note", str) is None


async def test_complete_preserves_metadata(serializer):
    backend = MemoryBackend()
    eid = _eid("root")

    async with TransactionScope(backend.transaction_provider):
        execution = Execution.start(eid)
        execution.set_metadata(
            "note", SerializedValue(await serializer.serialize("kept", str))
        )
        execution.complete(SerializedValue(await serializer.serialize("result", str)))
        await backend.repository.save(execution)

    record = await backend.repository.get(eid)
    assert record is not None and record.result is not None
    assert await serializer.deserialize(record.result.data, str) == "result"
    assert await _get_metadata(backend, serializer, eid, "note", str) == "kept"


async def test_delete_many_removes_metadata(serializer):
    backend = MemoryBackend()
    eid = _eid("root")
    await _start(backend, eid)
    async with TransactionScope(backend.transaction_provider):
        await _set_metadata(backend, serializer, eid, "note", "gone", str)

    async with TransactionScope(backend.transaction_provider):
        await backend.repository.delete_many([eid])

    assert await _get_metadata(backend, serializer, eid, "note", str) is None


async def test_set_metadata_unknown_execution_raises(serializer):
    backend = MemoryBackend()
    scope = TransactionScope(backend.transaction_provider)
    await scope.__aenter__()
    with pytest.raises(LookupError):
        await _set_metadata(backend, serializer, _eid("ghost"), "k", "v", str)
    await scope.rollback()


async def test_get_metadata_unknown_execution_returns_none(serializer):
    backend = MemoryBackend()
    assert await _get_metadata(backend, serializer, _eid("ghost"), "k", str) is None


async def test_ctx_metadata_roundtrips_and_persists(
    backend_factory: BackendFactory, hasher: ArgsHasher, serializer
):
    captured: dict[str, ExecutionId] = {}

    @engrave
    async def annotate() -> str:
        ctx = get_context()
        await ctx.set_metadata("trace", {"step": 1})
        assert await ctx.get_metadata("trace", dict) == {"step": 1}
        captured["id"] = ctx.current_execution_id  # type: ignore[assignment]
        return "done"

    backend = backend_factory("meta-ctx")
    async with Session(
        id="meta-ctx",
        repository=backend.repository,
        transaction_provider=backend.transaction_provider,
        serializer=serializer,
        hasher=hasher,
    ):
        result = await annotate()

    assert result == "done"
    assert await _get_metadata(backend, serializer, captured["id"], "trace", dict) == {
        "step": 1
    }


async def test_ctx_set_metadata_requires_active_execution():
    from glyff._context import Context, reset_context, set_context
    from glyff._event_system import EventEmitter
    from glyff._sequencer import Sequencer
    from glyff.serialization import JsonArgsHasher, JsonSerializer

    backend = MemoryBackend()
    ctx = Context(
        session_id="no-exec",
        repository=backend.repository,
        transaction_provider=backend.transaction_provider,
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
