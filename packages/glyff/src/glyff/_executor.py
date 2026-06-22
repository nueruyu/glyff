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
    Orchestrates the execution of a regular (awaitable) task, including cache
    checks, transaction management, state recording, and exception handling.
    """
    store = ctx.store
    sequencer = ctx.sequencer
    tracer = ctx.tracer

    record = await store.get_execution_record(execution_id, return_type)

    if record:
        if record.status == ExecutionStatus.COMPLETED:
            return record.result

    async with ctx.get_transaction_scope():
        # Reset child sequencers for deterministic re-execution.
        await sequencer.reset_for_call(execution_id)

        execution = await store.start_execution(execution_id)
        tracer.start(execution_id)

        try:
            result = await func(*args, **kwargs)
            await execution.complete(result, return_type)
            await ctx.event_emitter.emit(
                ExecutionCompleted(context=ctx, execution_id=execution_id)
            )
            return result
        except Exception as e:
            error_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            # Do not mark the execution as FAILED. Leaving it STARTED makes
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
