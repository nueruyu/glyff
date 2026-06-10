from unittest.mock import AsyncMock

import pytest

from glyff import EventEmitter, ExecutionId, ExecutionStatus, Serializer
from glyff._context import Context, TransactionScope, reset_context, set_context
from glyff._executor import execute
from glyff._sequencer import Sequencer
from glyff.event_handlers import PruningEventHandler
from glyff.exceptions import ExecutionFailedError, YieldException
from glyff.store._memory import _make_key
from glyff.store.helpers import execution_id_to_path
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
    assert len(mock_store.get_calls("commit")) == 1


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
        transaction_scope_factory=lambda: TransactionScope(mock_store),
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
        transaction_scope_factory=lambda: TransactionScope(mock_store),
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


async def test_failed_task_raises_error(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    executed = False

    async def sample_func():
        nonlocal executed
        executed = True

    # Setup the internal memory store to return a failed state
    path = execution_id_to_path(base_execution_id)
    mock_store._mem_store._client.data[_make_key(path, "status")] = (
        ExecutionStatus.FAILED
    )
    mock_store._mem_store._client.data[_make_key(path, "error")] = "it broke"

    with pytest.raises(ExecutionFailedError, match="failed previously"):
        await execute(
            ctx=test_context,
            execution_id=base_execution_id,
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )
    assert not executed


async def test_session_interrupted_skips_failure_staging(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        raise YieldException()

    with pytest.raises(YieldException):
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
    assert len(mock_store.get_calls("commit")) == 1
    assert not mock_store.get_calls("rollback")


async def test_general_exception_stages_failure(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func():
        raise ValueError("oops")

    with pytest.raises(ExecutionFailedError):
        await execute(
            ctx=test_context,
            execution_id=base_execution_id,
            func=sample_func,
            args=(),
            kwargs={},
            return_type=str,
        )

    assert not test_context.tracer.call_stack
    fail_calls = mock_store.get_calls("fail")
    assert len(fail_calls) == 1
    assert "ValueError" in fail_calls[0].args[1]

    assert len(mock_store.get_calls("commit")) == 1
    assert not mock_store.get_calls("rollback")


async def test_base_exception_triggers_rollback(
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

    assert not mock_store.get_calls("complete")
    assert not mock_store.get_calls("fail")
    assert not mock_store.get_calls("commit")
    assert len(mock_store.get_calls("rollback")) == 1
