from typing import Any, Callable

from ._context import Context
from ._models import (
    CanonicalArguments,
    Execution,
    ExecutionId,
    ExecutionStatus,
    SerializedValue,
)
from .events import ExecutionCompleted, ExecutionFailed


async def execute(
    ctx: Context,
    execution_id: ExecutionId,
    canonical_arguments: CanonicalArguments,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    return_type: type,
) -> Any:
    """
    Orchestrates the execution of a regular (awaitable) task: cache checks,
    per-event durable recording, and exception handling.

    START uses its own transaction so interrupted calls are retryable. The
    function body and COMPLETE share a transaction so metadata written through
    ctx.metadata commits atomically with the completed execution record.
    """
    repository = ctx.repository
    serializer = ctx.serializer
    sequencer = ctx.sequencer
    tracer = ctx.tracer

    session_id = ctx.session_id

    cached = await repository.get(session_id, execution_id)
    if cached is not None and cached.status == ExecutionStatus.COMPLETED:
        # A completed execution always carries a result; the aggregate enforces it.
        assert cached.result is not None
        return await serializer.deserialize(cached.result.data, return_type)

    async with ctx.get_transaction_scope():
        await sequencer.reset_for_call(execution_id)
        if await repository.get(session_id, execution_id) is None:
            await repository.save(
                session_id, Execution.start(execution_id, canonical_arguments)
            )

    tracer.start(execution_id)

    func_exception: Exception | None = None

    try:
        async with ctx.get_transaction_scope():
            try:
                result = await func(*args, **kwargs)
            except Exception as e:
                func_exception = e
                raise

            execution = await repository.get(session_id, execution_id)
            if execution is None:
                raise LookupError(f"Execution {execution_id} not found")

            serialized = await serializer.serialize(result, return_type)
            execution.complete(SerializedValue(serialized))
            await repository.save(session_id, execution)

        await ctx.event_emitter.emit(
            ExecutionCompleted(
                context=ctx,
                execution_id=execution_id,
            )
        )
        return result
    except Exception:
        if func_exception is not None:
            await ctx.event_emitter.emit(
                ExecutionFailed(
                    context=ctx,
                    execution_id=execution_id,
                    exception=func_exception,
                )
            )
        raise
    finally:
        tracer.end()
