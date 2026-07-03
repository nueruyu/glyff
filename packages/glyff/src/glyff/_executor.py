from typing import Any, Callable

from ._context import Context
from ._models import Execution, ExecutionId, ExecutionStatus, SerializedValue
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

    START, the function body, and COMPLETE each use separate transaction
    scopes, so a completed descendant can commit while an ancestor body is
    still running. ExecutionCompleted is emitted *after* the COMPLETE scope
    commits, so completion is durable before any handler runs and a handler
    (e.g. pruning/GC) must open its own transaction — its failure cannot roll
    back the completion.
    """
    executions = ctx.executions
    serializer = ctx.serializer
    sequencer = ctx.sequencer
    tracer = ctx.tracer

    cached = await executions.get(execution_id)
    if (
        cached is not None
        and cached.status == ExecutionStatus.COMPLETED
        and cached.result is not None
    ):
        return await serializer.deserialize(cached.result.data, return_type)

    async with ctx.get_transaction_scope():
        await sequencer.reset_for_call(execution_id)
        execution = await executions.get(execution_id)
        if execution is None or execution.status == ExecutionStatus.FAILED:
            execution = Execution.start(execution_id)
            await executions.save(execution)

    tracer.start(execution_id)
    try:
        async with ctx.get_transaction_scope() as scope:
            try:
                result = await func(*args, **kwargs)
            except Exception as e:
                execution = await executions.get(execution_id)
                if execution is None:
                    execution = Execution.start(execution_id)
                execution.fail(str(e))
                await executions.save(execution)
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
            execution = await executions.get(execution_id)
            if execution is None:
                raise LookupError(f"Execution {execution_id} not found")
            serialized = await serializer.serialize(result, return_type)
            execution.complete(SerializedValue(serialized))
            await executions.save(execution)

        # Emitted outside the COMPLETE scope: completion is already durable, so
        # a handler (pruning/GC) runs in its own transaction and cannot roll the
        # completion back.
        await ctx.event_emitter.emit(
            ExecutionCompleted(context=ctx, execution_id=execution_id)
        )
        return result
    finally:
        tracer.end()
