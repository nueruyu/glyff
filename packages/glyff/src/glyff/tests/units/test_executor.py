import logging
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
from glyff.store import MemoryExecutionRepository
from glyff.store._memory import _make_key
from glyff.store._memory_client import MemoryClient
from glyff.store.utils import execution_id_to_path
from glyff.testing import PruningEventHandler, canonical_arguments, make_execution_id
from glyff.tests.stubs.store import StubBackend, StubExecutionRepository


async def _result(backend: StubBackend, serializer, eid: ExecutionId, typ: type):
    execution = await backend.repository.get(eid)
    if execution is None or execution.result is None:
        return None
    return await serializer.deserialize(execution.result.data, typ)


@pytest.fixture
def transaction_scope_factory(mock_backend: StubBackend):
    def factory():
        return TransactionScope(mock_backend.transaction_provider)

    return factory


@pytest.fixture(autouse=True)
def set_context_for_tests(test_context: Context):
    token = set_context(test_context)
    yield
    reset_context(token)


async def test_successful_execution(
    mock_backend: StubBackend,
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
        canonical_arguments=canonical_arguments(),
        func=sample_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert result == "hello"
    assert not test_context.tracer.call_stack
    test_context.sequencer.reset_for_call.assert_called_once_with(base_execution_id)

    saves = mock_backend.get_calls("save")
    assert [c.args[0].status for c in saves] == [
        ExecutionStatus.STARTED,
        ExecutionStatus.COMPLETED,
    ]
    assert await _result(mock_backend, serializer, base_execution_id, str) == "hello"
    assert len(mock_backend.get_calls("commit")) == 2


async def test_completion_prunes_descendants_when_enabled(
    mock_backend: StubBackend,
    base_execution_id: ExecutionId,
    argument_canonicalizer,
    serializer: Serializer,
):
    emitter = EventEmitter([PruningEventHandler(mock_backend.repository)])
    ctx = Context(
        session_id="prune-on",
        backend=mock_backend,
        serializer=serializer,
        sequencer=Sequencer(),
        argument_canonicalizer=argument_canonicalizer,
        event_emitter=emitter,
    )
    token = set_context(ctx)
    try:
        child = make_execution_id("child", parent=base_execution_id)

        async def sample_func():
            async with ctx.get_transaction_scope():
                execution = Execution.start(child, canonical_arguments())
                execution.complete(
                    SerializedValue(await serializer.serialize("child", str))
                )
                await mock_backend.repository.save(execution)
            return "hello"

        result = await execute(
            ctx=ctx,
            execution_id=base_execution_id,
            canonical_arguments=canonical_arguments(),
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )
    finally:
        reset_context(token)

    assert result == "hello"
    enumerations = mock_backend.get_calls("executions")
    assert any(c.kwargs["under"] == base_execution_id for c in enumerations)
    delete_calls = mock_backend.get_calls("delete_many")
    assert len(delete_calls) == 1
    assert delete_calls[0].args[0] == [child]


async def test_nested_completion_prunes(
    mock_backend: StubBackend,
    nested_execution_id: ExecutionId,
    argument_canonicalizer,
    serializer: Serializer,
):
    emitter = EventEmitter([PruningEventHandler(mock_backend.repository)])
    ctx = Context(
        session_id="prune-nested",
        backend=mock_backend,
        serializer=serializer,
        sequencer=Sequencer(),
        argument_canonicalizer=argument_canonicalizer,
        event_emitter=emitter,
    )
    token = set_context(ctx)
    try:

        async def sample_func():
            return "hello"

        await execute(
            ctx=ctx,
            execution_id=nested_execution_id,
            canonical_arguments=canonical_arguments(),
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )
    finally:
        reset_context(token)

    enumerations = mock_backend.get_calls("executions")
    assert any(c.kwargs["under"] == nested_execution_id for c in enumerations)
    assert not mock_backend.get_calls("delete_many")


async def test_completion_does_not_prune_when_disabled(
    mock_backend: StubBackend,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        return "hello"

    await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        canonical_arguments=canonical_arguments(),
        func=sample_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert not mock_backend.get_calls("executions")
    assert not mock_backend.get_calls("delete_many")


async def test_completed_task_is_skipped(
    mock_backend: StubBackend,
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
    mock_backend._client.data[_make_key(path, "arguments")] = canonical_arguments().data
    mock_backend._client.data[_make_key(path, "status")] = ExecutionStatus.COMPLETED
    mock_backend._client.data[_make_key(path, "result")] = await serializer.serialize(
        "cached_result", str
    )

    result = await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        canonical_arguments=canonical_arguments(),
        func=sample_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert result == "cached_result"
    assert not executed
    test_context.sequencer.reset_for_call.assert_not_called()
    assert not mock_backend.get_calls("save")
    assert not mock_backend.get_calls("commit")


async def test_started_record_is_retryable(
    mock_backend: StubBackend,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        return "recovered"

    # A leftover STARTED record (an interrupted prior attempt) does not block
    # re-execution.
    path = execution_id_to_path(base_execution_id)
    mock_backend._client.data[_make_key(path, "arguments")] = canonical_arguments().data
    mock_backend._client.data[_make_key(path, "status")] = ExecutionStatus.STARTED

    result = await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        canonical_arguments=canonical_arguments(),
        func=sample_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert result == "recovered"


async def test_general_exception_persists_nothing(
    mock_backend: StubBackend,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        raise ValueError("oops")

    with pytest.raises(ValueError, match="oops"):
        await execute(
            ctx=test_context,
            execution_id=base_execution_id,
            canonical_arguments=canonical_arguments(),
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )

    assert not test_context.tracer.call_stack
    # Nothing is persisted on exception: the record stays STARTED (retried on
    # resume), the body scope rolled back, and no failure is written.
    record = await mock_backend.repository.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED
    assert len(mock_backend.get_calls("commit")) == 1
    assert len(mock_backend.get_calls("rollback")) == 1


async def test_application_pause_is_retryable(
    mock_backend: StubBackend,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    # Regression guard: an application-owned pause exception stamps no failure.
    # The record stays STARTED and the call completes when retried (resumed).
    class ApplicationPause(Exception):
        pass

    async def paused():
        raise ApplicationPause("waiting")

    with pytest.raises(ApplicationPause):
        await execute(
            ctx=test_context,
            execution_id=base_execution_id,
            canonical_arguments=canonical_arguments(),
            func=paused,
            args=(),
            kwargs={},
            return_type=str,
        )

    record = await mock_backend.repository.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED

    async def resumed():
        return "answer"

    result = await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        canonical_arguments=canonical_arguments(),
        func=resumed,
        args=(),
        kwargs={},
        return_type=str,
    )
    assert result == "answer"
    completed = await mock_backend.repository.get(base_execution_id)
    assert completed is not None
    assert completed.status == ExecutionStatus.COMPLETED


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
            canonical_arguments=canonical_arguments(),
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
    mock_backend: StubBackend,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        record = await mock_backend.repository.get(base_execution_id)
        assert record is not None
        assert record.status == ExecutionStatus.STARTED
        return "hello"

    result = await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        canonical_arguments=canonical_arguments(),
        func=sample_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert result == "hello"


async def test_function_exception_rolls_back_body_scope_writes(
    mock_backend: StubBackend,
    base_execution_id: ExecutionId,
    test_context: Context,
    serializer: Serializer,
):
    scratch_id = make_execution_id("scratch", parent=base_execution_id)

    async def sample_func():
        assert test_context.current_execution_id == base_execution_id
        execution = Execution.start(scratch_id, canonical_arguments())
        execution.complete(SerializedValue(await serializer.serialize("saved", str)))
        await test_context.repository.save(execution)
        raise ValueError("oops")

    with pytest.raises(ValueError, match="oops"):
        await execute(
            ctx=test_context,
            execution_id=base_execution_id,
            canonical_arguments=canonical_arguments(),
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )

    # Writes made directly in the failing body scope are rolled back — nothing
    # is persisted on exception.
    assert await mock_backend.repository.get(scratch_id) is None


async def test_failure_handler_can_clean_up_in_its_own_transaction(
    mock_backend: StubBackend,
    base_execution_id: ExecutionId,
    nested_execution_id: ExecutionId,
    argument_canonicalizer,
    serializer: Serializer,
):
    seen_exceptions: list[Exception] = []

    # ExecutionFailed is emitted after the body scope rolled back (no open
    # transaction), so a handler that wants to persist opens its own.
    class CleanupOnFailure(EventHandler[ExecutionFailed]):
        async def handle(self, event: ExecutionFailed) -> None:
            seen_exceptions.append(event.exception)
            async with event.context.get_transaction_scope():
                await event.context.repository.delete_many([nested_execution_id])

    ctx = Context(
        session_id="failure-handler-tx",
        backend=mock_backend,
        serializer=serializer,
        sequencer=Sequencer(),
        argument_canonicalizer=argument_canonicalizer,
        event_emitter=EventEmitter([CleanupOnFailure()]),
    )
    token = set_context(ctx)
    try:
        async with ctx.get_transaction_scope():
            execution = Execution.start(nested_execution_id, canonical_arguments())
            execution.complete(
                SerializedValue(await serializer.serialize("child", str))
            )
            await mock_backend.repository.save(execution)

        async def sample_func():
            raise ValueError("oops")

        with pytest.raises(ValueError, match="oops"):
            await execute(
                ctx=ctx,
                execution_id=base_execution_id,
                canonical_arguments=canonical_arguments(),
                func=sample_func,
                args=(),
                kwargs={},
                return_type=str,
            )
    finally:
        reset_context(token)

    delete_calls = mock_backend.get_calls("delete_many")
    assert len(delete_calls) == 1
    assert delete_calls[0].args[0] == [nested_execution_id]
    assert len(seen_exceptions) == 1
    assert isinstance(seen_exceptions[0], ValueError)
    assert str(seen_exceptions[0]) == "oops"
    assert await mock_backend.repository.get(nested_execution_id) is None
    record = await mock_backend.repository.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED


async def test_execution_failed_emits_after_body_transaction_closes(
    mock_backend: StubBackend,
    base_execution_id: ExecutionId,
    argument_canonicalizer,
    serializer: Serializer,
):
    scratch_id = make_execution_id("scratch", parent=base_execution_id)
    handler_saw_scratch: list[bool] = []
    write_errors: list[str] = []

    class ObserveFailure(EventHandler[ExecutionFailed]):
        async def handle(self, event: ExecutionFailed) -> None:
            mock_backend._record("failure_handler")
            handler_saw_scratch.append(
                await event.context.repository.get(scratch_id) is not None
            )
            try:
                await event.context.repository.delete_many([scratch_id])
            except RuntimeError as exc:
                write_errors.append(str(exc))

    ctx = Context(
        session_id="failure-handler-after-rollback",
        backend=mock_backend,
        serializer=serializer,
        sequencer=Sequencer(),
        argument_canonicalizer=argument_canonicalizer,
        event_emitter=EventEmitter([ObserveFailure()]),
    )
    token = set_context(ctx)
    try:

        async def sample_func():
            execution = Execution.start(scratch_id, canonical_arguments())
            execution.complete(
                SerializedValue(await serializer.serialize("scratch", str))
            )
            await ctx.repository.save(execution)
            raise ValueError("oops")

        with pytest.raises(ValueError, match="oops"):
            await execute(
                ctx=ctx,
                execution_id=base_execution_id,
                canonical_arguments=canonical_arguments(),
                func=sample_func,
                args=(),
                kwargs={},
                return_type=str,
            )
    finally:
        reset_context(token)

    call_names = [call.name for call in mock_backend.calls]
    assert call_names.index("rollback") < call_names.index("failure_handler")
    assert handler_saw_scratch == [False]
    assert write_errors == ["MemoryClient write attempted outside a transaction."]
    assert await mock_backend.repository.get(scratch_id) is None


async def test_failed_handler_failure_does_not_replace_original_exception(
    base_execution_id: ExecutionId,
    serializer: Serializer,
    argument_canonicalizer,
    caplog,
):
    class FailingFailedHandler(EventHandler[ExecutionFailed]):
        async def handle(self, event: ExecutionFailed) -> None:
            raise RuntimeError("handler failed")

    backend = StubBackend(client=MemoryClient())
    ctx = Context(
        session_id="failed-handler-fails",
        backend=backend,
        serializer=serializer,
        sequencer=Sequencer(),
        argument_canonicalizer=argument_canonicalizer,
        event_emitter=EventEmitter([FailingFailedHandler()]),
    )
    token = set_context(ctx)
    try:

        async def sample_func():
            raise ValueError("original")

        with caplog.at_level(logging.ERROR, logger="glyff._event_system"):
            with pytest.raises(ValueError, match="original"):
                await execute(
                    ctx=ctx,
                    execution_id=base_execution_id,
                    canonical_arguments=canonical_arguments(),
                    func=sample_func,
                    args=(),
                    kwargs={},
                    return_type=str,
                )
    finally:
        reset_context(token)

    assert "Event handler failed" in caplog.text
    assert "handler failed" in caplog.text
    record = await backend.repository.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED


async def test_execution_save_failure_rolls_back_complete_transaction(
    base_execution_id: ExecutionId,
    serializer: Serializer,
    argument_canonicalizer,
):
    class FailingCompleteRepository(StubExecutionRepository):
        async def save(self, execution: Execution) -> None:
            if execution.status is ExecutionStatus.COMPLETED:
                raise RuntimeError("complete failed")
            await super().save(execution)

    failed_events: list[ExecutionFailed] = []

    class RecordFailures(EventHandler[ExecutionFailed]):
        async def handle(self, event: ExecutionFailed) -> None:
            failed_events.append(event)

    client = MemoryClient()
    backend = StubBackend(client)
    backend.repository = FailingCompleteRepository(
        backend._record, MemoryExecutionRepository(client)
    )
    ctx = Context(
        session_id="complete-fails",
        backend=backend,
        serializer=serializer,
        sequencer=Sequencer(),
        argument_canonicalizer=argument_canonicalizer,
        event_emitter=EventEmitter([RecordFailures()]),
    )
    token = set_context(ctx)
    try:

        async def sample_func():
            await ctx.metadata.set("trace", {"step": 1})
            return "hello"

        with pytest.raises(RuntimeError, match="complete failed"):
            await execute(
                ctx=ctx,
                execution_id=base_execution_id,
                canonical_arguments=canonical_arguments(),
                func=sample_func,
                args=(),
                kwargs={},
                return_type=str,
            )
    finally:
        reset_context(token)

    record = await backend.repository.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED
    assert record.get_metadata("trace") is None
    assert failed_events == []
    assert len(backend.get_calls("rollback")) == 1


async def test_completed_handler_failure_does_not_affect_result_or_completion(
    base_execution_id: ExecutionId,
    serializer: Serializer,
    argument_canonicalizer,
    caplog,
):
    class FailingCompletedHandler(EventHandler[ExecutionCompleted]):
        async def handle(self, event: ExecutionCompleted) -> None:
            raise RuntimeError("handler failed")

    backend = StubBackend(client=MemoryClient())
    ctx = Context(
        session_id="completed-handler-fails",
        backend=backend,
        serializer=serializer,
        sequencer=Sequencer(),
        argument_canonicalizer=argument_canonicalizer,
        event_emitter=EventEmitter([FailingCompletedHandler()]),
    )
    token = set_context(ctx)
    try:

        async def sample_func():
            return "hello"

        with caplog.at_level(logging.ERROR, logger="glyff._event_system"):
            result = await execute(
                ctx=ctx,
                execution_id=base_execution_id,
                canonical_arguments=canonical_arguments(),
                func=sample_func,
                args=(),
                kwargs={},
                return_type=str,
            )
    finally:
        reset_context(token)

    assert result == "hello"
    record = await backend.repository.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.COMPLETED
    assert not backend.get_calls("rollback")
    assert "Event handler failed" in caplog.text
    assert "handler failed" in caplog.text


async def test_nested_child_commits_without_losing_parent_staging(
    mock_backend: StubBackend,
    base_execution_id: ExecutionId,
    nested_execution_id: ExecutionId,
    test_context: Context,
):
    marker_id = make_execution_id("marker", parent=base_execution_id)

    async def child_func():
        return "child"

    async def parent_func():
        await test_context.repository.save(
            Execution.start(marker_id, canonical_arguments())
        )
        child = await execute(
            ctx=test_context,
            execution_id=nested_execution_id,
            canonical_arguments=canonical_arguments(),
            func=child_func,
            args=(),
            kwargs={},
            return_type=str,
        )
        marker = await test_context.repository.get(marker_id)
        assert marker is not None
        assert marker.status == ExecutionStatus.STARTED
        child_record = await test_context.repository.get(nested_execution_id)
        assert child_record is not None
        assert child_record.status == ExecutionStatus.COMPLETED
        return child

    result = await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        canonical_arguments=canonical_arguments(),
        func=parent_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert result == "child"
    marker = await mock_backend.repository.get(marker_id)
    assert marker is not None
    assert marker.status == ExecutionStatus.STARTED
    child_record = await mock_backend.repository.get(nested_execution_id)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED


async def test_base_exception_after_start_keeps_started_record(
    mock_backend: StubBackend,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        raise KeyboardInterrupt("Ctrl+C")

    with pytest.raises(KeyboardInterrupt):
        await execute(
            ctx=test_context,
            execution_id=base_execution_id,
            canonical_arguments=canonical_arguments(),
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )

    assert len(mock_backend.get_calls("commit")) == 1
    assert len(mock_backend.get_calls("rollback")) == 1

    record = await mock_backend.repository.get(base_execution_id)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED
