import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Callable

from .context import Context
from .exceptions import ExecutionFailedError, YieldException
from .models import ExecutionStatus, ReturnTypeInfo


@asynccontextmanager
async def _execution_context(ctx: Context, execution_id: Any):
    """
    Manages the lifecycle of a single execution attempt: setup, teardown,
    and centralized exception handling.

    Yields the Execution object for recording outcomes.
    """
    await ctx.sequencer.reset_for_call(execution_id)
    execution = await ctx.store.start_execution(execution_id)
    ctx.tracer.start(execution_id)
    try:
        yield execution
    except YieldException:
        # Graceful interruption: state remains STARTED for resumption.
        raise
    except Exception as e:
        error_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        await execution.fail(error_str)
        raise ExecutionFailedError(
            f"Task {execution_id} failed: {type(e).__name__}({e})"
        ) from e
    finally:
        ctx.tracer.end()


async def execute(
    ctx: Context,
    execution_id: Any,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    type_info: ReturnTypeInfo,
) -> Any:
    """
    Orchestrates the execution of a task by dispatching to the appropriate
    handler based on the return type (single value or stream).
    """
    record = await ctx.store.get_execution_record(execution_id, type_info.full_type)

    if record:
        if record.status == ExecutionStatus.COMPLETED:
            if type_info.is_streaming:
                return ctx.store.get_stream_items(execution_id, type_info.item_type)
            return record.result
        if record.status == ExecutionStatus.FAILED:
            original_error = Exception(
                record.error or "Unknown previously failed error"
            )
            raise ExecutionFailedError(
                f"Task {execution_id} failed previously and cannot be re-executed."
            ) from original_error

    if type_info.is_streaming:
        return _execute_streaming_func(ctx, execution_id, func, args, kwargs, type_info)
    else:
        return await _execute_single_value_func(
            ctx, execution_id, func, args, kwargs, type_info
        )


async def _execute_single_value_func(
    ctx: Context,
    execution_id: Any,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    type_info: ReturnTypeInfo,
) -> Any:
    """Handles the execution of a function that returns a single value."""
    async with ctx.get_transaction_scope():
        async with _execution_context(ctx, execution_id) as execution:
            result = await func(*args, **kwargs)
            await execution.complete(result, type_info.full_type)
            return result


async def _execute_streaming_func(
    ctx: Context,
    execution_id: Any,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    type_info: ReturnTypeInfo,
) -> AsyncIterator[Any]:
    """Handles the execution of a function that returns an AsyncIterator."""
    async with ctx.get_transaction_scope():
        async with _execution_context(ctx, execution_id) as execution:
            # Phase 1: Replay any previously yielded items
            yielded_count = 0
            async for item in ctx.store.get_stream_items(
                execution_id, type_info.item_type
            ):
                yield item
                yielded_count += 1

            # Phase 2: Execute the function, skip already-replayed items
            skipped = 0
            async for item in func(*args, **kwargs):
                if skipped < yielded_count:
                    skipped += 1
                    continue
                await execution.yield_item(item, type_info.item_type)
                yield item

            await execution.complete_stream()
