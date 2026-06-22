import traceback
from typing import Any, Callable

from ._context import Context
from ._models import ExecutionId, ExecutionStatus
from .events import ExecutionCompleted, ExecutionFailed


async def execute(
    ctx: Context,
    execution_id: ExecutionId,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    return_type: type,
) -> Any:
    """
    Orchestrates the execution of a regular (awaitable) task: cache checks,
    per-event durable recording, and exception handling.

    Each execution event is persisted in its own store transaction as it
    happens — the START record before the function runs and the COMPLETE record
    when it returns — so a completed descendant survives a later interruption or
    crash of an ancestor.
    """
    store = ctx.store
    sequencer = ctx.sequencer
    tracer = ctx.tracer

    record = await store.get_execution_record(execution_id, return_type)

    if record:
        if record.status == ExecutionStatus.COMPLETED:
            return record.result

    # Reset child sequencers for deterministic re-execution.
    await sequencer.reset_for_call(execution_id)

    # Persist the START record durably before running the function, so a
    # completed descendant is not lost if an ancestor is later interrupted.
    async with ctx.get_transaction_scope():
        execution = await store.start_execution(execution_id)

    tracer.start(execution_id)
    try:
        result = await func(*args, **kwargs)
        # Persist the COMPLETE record (and run completion handlers such as
        # pruning) durably in its own transaction.
        async with ctx.get_transaction_scope():
            await execution.complete(result, return_type)
            await ctx.event_emitter.emit(
                ExecutionCompleted(context=ctx, execution_id=execution_id)
            )
        return result
    except Exception as e:
        error_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        # Do not mark the execution as FAILED. The committed START record keeps
        # the call retryable on resume, matching crash/kill behavior.
        await ctx.event_emitter.emit(
            ExecutionFailed(
                context=ctx,
                execution_id=execution_id,
                error=error_str,
            )
        )
        raise
    finally:
        tracer.end()
