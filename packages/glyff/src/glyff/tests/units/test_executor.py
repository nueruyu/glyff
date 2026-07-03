import traceback
from unittest.mock import AsyncMock

import pytest

from glyff import EventEmitter, Execution, ExecutionId, ExecutionStatus, Serializer
from glyff._context import Context, TransactionScope, reset_context, set_context
from glyff._event_system import EventHandler
from glyff._executor import execute
from glyff._sequencer import Sequencer
from glyff.events import ExecutionCompleted, ExecutionFailed
from glyff.store._memory_client import MemoryClient
from glyff.store._memory import _make_key
from glyff.store.utils import execution_id_to_path
from glyff.tests.stubs.pruning import PruningEventHandler
from glyff.tests.stubs.store import StubSessionStore


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
    assert len(mock_store.get_calls("start_execution")) == 1
    assert mock_store.get_calls("start_execution")[0].args[0] == base_execution_id

    complete_calls = mock_store.get_calls("complete")
    assert len(complete_calls) == 1
    assert complete_calls[0].args == (base_execution_id, "hello", str)

    assert not mock_store.get_calls("fail")
    # START, body, and COMPLETE each have their own transaction boundary.
    assert len(mock_store.get_calls("commit")) == 3


async def test_completion_prunes_descendants_when_enabled(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    hasher,
):
    # A context with pruning handler registered.
    emitter = EventEmitter([PruningEventHandler()])
    ctx = Context(
        session_id="prune-on",
        store=mock_store,
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
            # Record the child so it becomes a real descendant in the store.
            async with ctx.get_transaction_scope():
                execution = await mock_store.start_execution(child)
                await execution.complete("child", str)
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
    # The executor asked the store for the parent's descendants and deleted the
    # child it found.
    desc_calls = mock_store.get_calls("get_descendants")
    assert any(c.args[0] == base_execution_id for c in desc_calls)
    delete_calls = mock_store.get_calls("delete_executions")
    # A single batched call deleting exactly the child descendant.
    assert len(delete_calls) == 1
    assert delete_calls[0].args[0] == [child]


async def test_nested_completion_prunes(
    mock_store: StubSessionStore,
    nested_execution_id: ExecutionId,
    hasher,
):
    # Pruning fires at every completion, including nested ones: a completed
    # nested call scans for its own descendants right away rather than waiting
    # for its top-level ancestor to finish.
    emitter = EventEmitter([PruningEventHandler()])
    ctx = Context(
        session_id="prune-nested",
        store=mock_store,
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

    # The nested completion scanned for its own descendants...
    desc_calls = mock_store.get_calls("get_descendants")
    assert any(c.args[0] == nested_execution_id for c in desc_calls)
    # ...but found none here, so nothing was deleted.
    assert not mock_store.get_calls("delete_executions")


async def test_completion_does_not_prune_when_disabled(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    # Default test_context has no pruning handler registered.
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

    assert not mock_store.get_calls("get_descendants")
    assert not mock_store.get_calls("delete_executions")


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

    # Setup the internal memory store to return a completed state
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
    assert not mock_store.get_calls("start_execution")
    assert not mock_store.get_calls("commit")


async def test_failed_record_is_retryable(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    executed = False

    async def sample_func():
        nonlocal executed
        executed = True
        return "recovered"

    # A leftover FAILED record no longer gates re-execution; the call is retried.
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
    assert executed


async def test_interrupting_exception_skips_failure_staging(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    class ApplicationPause(Exception):
        pass

    async def sample_func():
        raise ApplicationPause()

    with pytest.raises(ApplicationPause):
        await execute(
            ctx=test_context,
            execution_id=base_execution_id,
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )

    assert not test_context.tracer.call_stack
    assert not mock_store.get_calls("complete")
    assert not mock_store.get_calls("fail")
    # One commit for START, one explicit body commit after ExecutionFailed.
    assert len(mock_store.get_calls("commit")) == 2
    assert not mock_store.get_calls("rollback")


async def test_general_exception_is_non_terminal(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        raise ValueError("oops")

    # The original exception propagates unwrapped (not ExecutionFailedError).
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
    # No failure is staged; the call stays STARTED so it is retryable on resume.
    assert not mock_store.get_calls("fail")
    assert len(mock_store.get_calls("start_execution")) == 1
    record = await mock_store.get_execution_record(base_execution_id, str)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED
    assert len(mock_store.get_calls("commit")) == 2
    assert not mock_store.get_calls("rollback")


async def test_original_traceback_is_preserved_on_function_exception(
    mock_store: StubSessionStore,
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
        record = await mock_store.get_execution_record(base_execution_id, str)
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
):
    metadata_id = ExecutionId(
        parent_id=base_execution_id,
        name="metadata",
        sequence=0,
        args_hash="state",
    )

    async def sample_func():
        assert test_context.current_execution_id == base_execution_id
        execution = await test_context.store.start_execution(metadata_id)
        await execution.complete("saved", str)
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

    record = await mock_store.get_execution_record(metadata_id, str)
    assert record is not None
    assert record.status == ExecutionStatus.COMPLETED
    assert record.result == "saved"


async def test_failure_event_handlers_run_inside_transaction(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    nested_execution_id: ExecutionId,
    hasher,
):
    seen_exceptions: list[Exception] = []

    class CleanupOnFailure(EventHandler[ExecutionFailed]):
        async def handle(self, event: ExecutionFailed) -> None:
            seen_exceptions.append(event.exception)
            await event.context.store.repository.delete_executions(
                [nested_execution_id]
            )

    ctx = Context(
        session_id="failure-handler-tx",
        store=mock_store,
        sequencer=Sequencer(),
        hasher=hasher,
        event_emitter=EventEmitter([CleanupOnFailure()]),
    )
    token = set_context(ctx)
    try:
        async with ctx.get_transaction_scope():
            execution = await mock_store.start_execution(nested_execution_id)
            await execution.complete("child", str)

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

    delete_calls = mock_store.get_calls("delete_executions")
    assert len(delete_calls) == 1
    assert delete_calls[0].args[0] == [nested_execution_id]
    assert len(seen_exceptions) == 1
    assert isinstance(seen_exceptions[0], ValueError)
    assert str(seen_exceptions[0]) == "oops"
    assert await mock_store.get_execution_record(nested_execution_id, str) is None
    record = await mock_store.get_execution_record(base_execution_id, str)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED


async def test_execution_complete_failure_rolls_back_complete_transaction(
    base_execution_id: ExecutionId,
    serializer: Serializer,
    hasher,
):
    class FailingCompleteExecution(Execution):
        def __init__(self, inner: Execution):
            self._inner = inner

        async def complete(self, value, return_type) -> None:
            await self._inner.complete(value, return_type)
            raise RuntimeError("complete failed")

        async def fail(self, error: str) -> None:
            await self._inner.fail(error)

    class FailingCompleteStore(StubSessionStore):
        async def start_execution(self, execution_id: ExecutionId) -> Execution:
            execution = await super().start_execution(execution_id)
            return FailingCompleteExecution(execution)

    store = FailingCompleteStore(client=MemoryClient(), serializer=serializer)
    ctx = Context(
        session_id="complete-fails",
        store=store,
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

    record = await store.get_execution_record(base_execution_id, str)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED
    assert len(store.get_calls("rollback")) == 1


async def test_completed_handler_failure_does_not_roll_back_completion(
    base_execution_id: ExecutionId,
    serializer: Serializer,
    hasher,
):
    # ExecutionCompleted is emitted after the COMPLETE scope commits, so a
    # failing completed-handler propagates but cannot roll the durable
    # completion back.
    class FailingCompletedHandler(EventHandler[ExecutionCompleted]):
        async def handle(self, event: ExecutionCompleted) -> None:
            raise RuntimeError("handler failed")

    store = StubSessionStore(client=MemoryClient(), serializer=serializer)
    ctx = Context(
        session_id="completed-handler-fails",
        store=store,
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

    record = await store.get_execution_record(base_execution_id, str)
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
        await test_context.store.start_execution(marker_id)
        child = await execute(
            ctx=test_context,
            execution_id=nested_execution_id,
            func=child_func,
            args=(),
            kwargs={},
            return_type=str,
        )
        marker = await test_context.store.get_execution_record(marker_id, str)
        assert marker is not None
        assert marker.status == ExecutionStatus.STARTED
        child_record = await test_context.store.get_execution_record(
            nested_execution_id, str
        )
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
    marker = await mock_store.get_execution_record(marker_id, str)
    assert marker is not None
    assert marker.status == ExecutionStatus.STARTED
    child_record = await mock_store.get_execution_record(nested_execution_id, str)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED


async def test_parent_staging_is_restored_after_nested_child_rolls_back(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    nested_execution_id: ExecutionId,
    test_context: Context,
):
    class ChildAbort(BaseException):
        pass

    marker_id = ExecutionId(
        parent_id=base_execution_id,
        name="marker",
        sequence=0,
        args_hash="parent",
    )

    async def child_func():
        raise ChildAbort()

    async def parent_func():
        await test_context.store.start_execution(marker_id)
        with pytest.raises(ChildAbort):
            await execute(
                ctx=test_context,
                execution_id=nested_execution_id,
                func=child_func,
                args=(),
                kwargs={},
                return_type=str,
            )
        marker = await test_context.store.get_execution_record(marker_id, str)
        assert marker is not None
        assert marker.status == ExecutionStatus.STARTED
        return "parent"

    result = await execute(
        ctx=test_context,
        execution_id=base_execution_id,
        func=parent_func,
        args=(),
        kwargs={},
        return_type=str,
    )

    assert result == "parent"
    marker = await mock_store.get_execution_record(marker_id, str)
    assert marker is not None
    assert marker.status == ExecutionStatus.STARTED
    child_record = await mock_store.get_execution_record(nested_execution_id, str)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.STARTED


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

    # The START record is committed in its own transaction before the function
    # runs, so a BaseException raised during the body leaves it durably STARTED
    # (retryable on resume) rather than rolling it back.
    assert len(mock_store.get_calls("start_execution")) == 1
    assert len(mock_store.get_calls("commit")) == 1
    assert not mock_store.get_calls("complete")
    assert not mock_store.get_calls("fail")
    assert len(mock_store.get_calls("rollback")) == 1

    record = await mock_store.get_execution_record(base_execution_id, str)
    assert record is not None
    assert record.status == ExecutionStatus.STARTED
