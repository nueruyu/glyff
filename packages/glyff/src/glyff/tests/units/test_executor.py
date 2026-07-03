import traceback
from unittest.mock import AsyncMock

import pytest

from glyff import (
    EventEmitter,
    Execution,
    ExecutionId,
    ExecutionStatus,
    SerializedValue,
    Serializer,
)
from glyff._context import Context, TransactionScope, reset_context, set_context
from glyff._event_system import EventHandler
from glyff._executor import execute
from glyff._sequencer import Sequencer
from glyff.events import ExecutionCompleted, ExecutionFailed
from glyff.store._memory import _make_key
from glyff.store._memory_client import MemoryClient
from glyff.store.utils import execution_id_to_path
from glyff.tests.stubs.pruning import PruningEventHandler
from glyff.tests.stubs.store import StubSessionStore


async def _result(store: StubSessionStore, serializer, eid: ExecutionId, typ: type):
    execution = await store.get(eid)
    if execution is None or execution.result is None:
        return None
    return await serializer.deserialize(execution.result.data, typ)


@pytest.fixture
def transaction_scope_factory(mock_store: StubSessionStore):
    def factory():
        return TransactionScope(mock_store)

    return factory


@pytest.fixture(autouse=True)
def set_context_for_tests(test_context: Context):
    token = set_context(test_context)
    yield
    reset_context(token)


async def test_successful_execution(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
    serializer: Serializer,
):
    async def sample_func():
        return "hello"

    test_context.sequencer.reset_for_call = AsyncMock()

    result = await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        func=sample_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert result == "hello"
    assert not test_context.tracer.call_stack
    test_context.sequencer.reset_for_call.assert_called_once_with(base_execution_id)

    saves = mock_store.get_calls("save")
    assert [c.args[0].status for c in saves] == [
        ExecutionStatus.STARTED,
        ExecutionStatus.COMPLETED,
    ]
    assert await _result(mock_store, serializer, base_execution_id, str) == "hello"
    assert len(mock_store.get_calls("commit")) == 3


async def test_completion_prunes_descendants_when_enabled(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    hasher,
    serializer: Serializer,
):
    emitter = EventEmitter([PruningEventHandler()])
    ctx = Context(
        session_id="prune-on",
        executions=mock_store,
        transactions=mock_store,
        serializer=serializer,
        sequencer=Sequencer(),
        hasher=hasher,
        event_emitter=emitter,
    )
    token = set_context(ctx)
    try:
        child = ExecutionId(
            parent_id=base_execution_id, name="child", sequence=0, args_hash="c"
        )

        async def sample_func():
            async with ctx.get_transaction_scope():
                execution = Execution.start(child)
                execution.complete(
                    SerializedValue(await serializer.serialize("child", str))
                )
                await mock_store.save(execution)
            return "hello"

        result = await execute(
            ctx=ctx,
            execution_id=base_execution_id,
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )
    finally:
        reset_context(token)

    assert result == "hello"
    desc_calls = mock_store.get_calls("descendants_of")
    assert any(c.args[0] == base_execution_id for c in desc_calls)
    delete_calls = mock_store.get_calls("delete_many")
    assert len(delete_calls) == 1
    assert delete_calls[0].args[0] == [child]


async def test_nested_completion_prunes(
    mock_store: StubSessionStore,
    nested_execution_id: ExecutionId,
    hasher,
    serializer: Serializer,
):
    emitter = EventEmitter([PruningEventHandler()])
    ctx = Context(
        session_id="prune-nested",
        executions=mock_store,
        transactions=mock_store,
        serializer=serializer,
        sequencer=Sequencer(),
        hasher=hasher,
        event_emitter=emitter,
    )
    token = set_context(ctx)
    try:
        async def sample_func():
            return "hello"

        await execute(
            ctx=ctx,
            execution_id=nested_execution_id,
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )
    finally:
        reset_context(token)

    desc_calls = mock_store.get_calls("descendants_of")
    assert any(c.args[0] == nested_execution_id for c in desc_calls)
    assert not mock_store.get_calls("delete_many")


async def test_completion_does_not_prune_when_disabled(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        return "hello"

    await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        func=sample_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert not mock_store.get_calls("descendants_of")
    assert not mock_store.get_calls("delete_many")


async def test_completed_task_is_skipped(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
    serializer: Serializer,
):
    executed = False

    async def sample_func():
        nonlocal executed
        executed = True

    test_context.sequencer.reset_for_call = AsyncMock()

    path = execution_id_to_path(base_execution_id)
    mock_store._mem_store._client.data[_make_key(path, "status")] = (
        ExecutionStatus.COMPLETED
    )
    mock_store._mem_store._client.data[
        _make_key(path, "result")
    ] = await serializer.serialize("cached_result", str)

    result = await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        func=sample_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert result == "cached_result"
    assert not executed
    test_context.sequencer.reset_for_call.assert_not_called()
    assert not mock_store.get_calls("save")
    assert not mock_store.get_calls("commit")


async def test_failed_record_is_retryable(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        return "recovered"

    path = execution_id_to_path(base_execution_id)
    mock_store._mem_store._client.data[_make_key(path, "status")] = (
        ExecutionStatus.FAILED
    )
    mock_store._mem_store._client.data[_make_key(path, "error")] = "it broke"

    result = await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        func=sample_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert result == "recovered"


async def test_general_exception_marks_execution_failed(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        raise ValueError("oops")

    with pytest.raises(ValueError, match="oops"):
        await execute(
            ctx=test_context,
            execution_id=base_execution_id,
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )

    assert not test_context.tracer.call_stack
    record = await mock_store.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.FAILED
    assert record.error == "oops"
    assert len(mock_store.get_calls("commit")) == 2
    assert not mock_store.get_calls("rollback")


async def test_original_traceback_is_preserved_on_function_exception(
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def leaf():
        raise ValueError("origin")

    async def sample_func():
        await leaf()

    with pytest.raises(ValueError, match="origin") as exc_info:
        await execute(
            ctx=test_context,
            execution_id=base_execution_id,
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )

    frame_names = [
        frame.name for frame in traceback.extract_tb(exc_info.value.__traceback__)
    ]
    assert "leaf" in frame_names


async def test_start_is_committed_before_function_body_runs(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        record = await mock_store.get(base_execution_id)
        assert record is not None
        assert record.status == ExecutionStatus.STARTED
        return "hello"

    result = await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        func=sample_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert result == "hello"


async def test_function_exception_commits_body_scope_writes(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
    serializer: Serializer,
):
    metadata_id = ExecutionId(
        parent_id=base_execution_id,
        name="metadata",
        sequence=0,
        args_hash="state",
    )

    async def sample_func():
        assert test_context.current_execution_id == base_execution_id
        execution = Execution.start(metadata_id)
        execution.complete(SerializedValue(await serializer.serialize("saved", str)))
        await test_context.executions.save(execution)
        raise ValueError("oops")

    with pytest.raises(ValueError, match="oops"):
        await execute(
            ctx=test_context,
            execution_id=base_execution_id,
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )

    record = await mock_store.get(metadata_id)
    assert record is not None
    assert record.status == ExecutionStatus.COMPLETED
    assert await _result(mock_store, serializer, metadata_id, str) == "saved"


async def test_failure_event_handlers_run_inside_transaction(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    nested_execution_id: ExecutionId,
    hasher,
    serializer: Serializer,
):
    seen_exceptions: list[Exception] = []

    class CleanupOnFailure(EventHandler[ExecutionFailed]):
        async def handle(self, event: ExecutionFailed) -> None:
            seen_exceptions.append(event.exception)
            await event.context.executions.delete_many([nested_execution_id])

    ctx = Context(
        session_id="failure-handler-tx",
        executions=mock_store,
        transactions=mock_store,
        serializer=serializer,
        sequencer=Sequencer(),
        hasher=hasher,
        event_emitter=EventEmitter([CleanupOnFailure()]),
    )
    token = set_context(ctx)
    try:
        async with ctx.get_transaction_scope():
            execution = Execution.start(nested_execution_id)
            execution.complete(SerializedValue(await serializer.serialize("child", str)))
            await mock_store.save(execution)

        async def sample_func():
            raise ValueError("oops")

        with pytest.raises(ValueError, match="oops"):
            await execute(
                ctx=ctx,
                execution_id=base_execution_id,
                func=sample_func,
                args=(),
                kwargs={},
                return_type=str,
            )
    finally:
        reset_context(token)

    delete_calls = mock_store.get_calls("delete_many")
    assert len(delete_calls) == 1
    assert delete_calls[0].args[0] == [nested_execution_id]
    assert len(seen_exceptions) == 1
    assert isinstance(seen_exceptions[0], ValueError)
    assert str(seen_exceptions[0]) == "oops"
    assert await mock_store.get(nested_execution_id) is None
    record = await mock_store.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.FAILED


async def test_execution_save_failure_rolls_back_complete_transaction(
    base_execution_id: ExecutionId,
    serializer: Serializer,
    hasher,
):
    class FailingCompleteStore(StubSessionStore):
        async def save(self, execution: Execution) -> None:
            if execution.status is ExecutionStatus.COMPLETED:
                raise RuntimeError("complete failed")
            await super().save(execution)

    store = FailingCompleteStore(client=MemoryClient(), serializer=serializer)
    ctx = Context(
        session_id="complete-fails",
        executions=store,
        transactions=store,
        serializer=serializer,
        sequencer=Sequencer(),
        hasher=hasher,
        event_emitter=EventEmitter([]),
    )
    token = set_context(ctx)
    try:
        async def sample_func():
            return "hello"

        with pytest.raises(RuntimeError, match="complete failed"):
            await execute(
                ctx=ctx,
                execution_id=base_execution_id,
                func=sample_func,
                args=(),
                kwargs={},
                return_type=str,
            )
    finally:
        reset_context(token)

    record = await store.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED
    assert len(store.get_calls("rollback")) == 1


async def test_completed_handler_failure_does_not_roll_back_completion(
    base_execution_id: ExecutionId,
    serializer: Serializer,
    hasher,
):
    class FailingCompletedHandler(EventHandler[ExecutionCompleted]):
        async def handle(self, event: ExecutionCompleted) -> None:
            raise RuntimeError("handler failed")

    store = StubSessionStore(client=MemoryClient(), serializer=serializer)
    ctx = Context(
        session_id="completed-handler-fails",
        executions=store,
        transactions=store,
        serializer=serializer,
        sequencer=Sequencer(),
        hasher=hasher,
        event_emitter=EventEmitter([FailingCompletedHandler()]),
    )
    token = set_context(ctx)
    try:
        async def sample_func():
            return "hello"

        with pytest.raises(RuntimeError, match="handler failed"):
            await execute(
                ctx=ctx,
                execution_id=base_execution_id,
                func=sample_func,
                args=(),
                kwargs={},
                return_type=str,
            )
    finally:
        reset_context(token)

    record = await store.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.COMPLETED
    assert not store.get_calls("rollback")


async def test_nested_child_commits_without_losing_parent_staging(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    nested_execution_id: ExecutionId,
    test_context: Context,
):
    marker_id = ExecutionId(
        parent_id=base_execution_id,
        name="marker",
        sequence=0,
        args_hash="parent",
    )

    async def child_func():
        return "child"

    async def parent_func():
        await test_context.executions.save(Execution.start(marker_id))
        child = await execute(
            ctx=test_context,
            execution_id=nested_execution_id,
            func=child_func,
            args=(),
            kwargs={},
            return_type=str,
        )
        marker = await test_context.executions.get(marker_id)
        assert marker is not None
        assert marker.status == ExecutionStatus.STARTED
        child_record = await test_context.executions.get(nested_execution_id)
        assert child_record is not None
        assert child_record.status == ExecutionStatus.COMPLETED
        return child

    result = await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        func=parent_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert result == "child"
    marker = await mock_store.get(marker_id)
    assert marker is not None
    assert marker.status == ExecutionStatus.STARTED
    child_record = await mock_store.get(nested_execution_id)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED


async def test_base_exception_after_start_keeps_started_record(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        raise KeyboardInterrupt("Ctrl+C")

    with pytest.raises(KeyboardInterrupt):
        await execute(
            ctx=test_context,
            execution_id=base_execution_id,
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )

    assert len(mock_store.get_calls("commit")) == 1
    assert len(mock_store.get_calls("rollback")) == 1

    record = await mock_store.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED
