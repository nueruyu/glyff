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
    if record and record.status == ExecutionStatus.COMPLETED:
        return record.result

    async with ctx.get_transaction_scope():
        await sequencer.reset_for_call(execution_id)
        execution = await store.start_execution(execution_id)

    tracer.start(execution_id)
    try:
        result = await func(*args, **kwargs)

        async with ctx.get_transaction_scope():
            await execution.complete(result, return_type)
            await ctx.event_emitter.emit(
                ExecutionCompleted(context=ctx, execution_id=execution_id)
            )
        return result
    except Exception as e:
        async with ctx.get_transaction_scope():
            await ctx.event_emitter.emit(
                ExecutionFailed(
                    context=ctx,
                    execution_id=execution_id,
                    exception=e,
                )
            )
        raise
    finally:
        tracer.end()
