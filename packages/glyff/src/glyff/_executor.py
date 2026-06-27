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

    START, the function body, and COMPLETE each use separate store transaction
    scopes, so a completed descendant can commit while an ancestor body is
    still running.
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
        async with ctx.get_transaction_scope() as scope:
            try:
                result = await func(*args, **kwargs)
            except Exception as e:
                await ctx.event_emitter.emit(
                    ExecutionFailed(
                        context=ctx,
                        execution_id=execution_id,
                        exception=e,
                    )
                )
                await scope.commit()
                raise

        async with ctx.get_transaction_scope():
            await execution.complete(result, return_type)
            await ctx.event_emitter.emit(
                ExecutionCompleted(context=ctx, execution_id=execution_id)
            )
        return result
    finally:
        tracer.end()
