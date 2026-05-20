from unittest.mock import AsyncMock

import pytest

from glyff.context import Context, TransactionScope, reset_context, set_context
from glyff.exceptions import ExecutionFailedError, YieldException
from glyff.executor import execute
from glyff.interfaces import Serializer
from glyff.models import ExecutionId, ExecutionStatus
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
    key_prefix = f"execution::{base_execution_id.name}#{base_execution_id.sequence}:{base_execution_id.args_hash}"
    mock_store._mem_store._client.data[f"{key_prefix}::status"] = (
        ExecutionStatus.COMPLETED
    )
    mock_store._mem_store._client.data[f"{key_prefix}::result"] = serializer.serialize(
        "cached_result", str
    )

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
    key_prefix = f"execution::{base_execution_id.name}#{base_execution_id.sequence}:{base_execution_id.args_hash}"
    mock_store._mem_store._client.data[f"{key_prefix}::status"] = ExecutionStatus.FAILED
    mock_store._mem_store._client.data[f"{key_prefix}::error"] = "it broke"

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
