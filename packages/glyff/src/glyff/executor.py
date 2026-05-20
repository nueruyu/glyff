import traceback
from typing import Any, Callable

from .context import Context
from .exceptions import ExecutionFailedError, YieldException
from .models import ExecutionStatus


async def execute(
    ctx: Context,
    execution_id: Any,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    return_type: type,
) -> Any:
    """
    Orchestrates the execution of a task, including cache checks, transaction
    management, state recording, and exception handling.
    """
    store = ctx.store
    sequencer = ctx.sequencer
    tracer = ctx.tracer

    record = await store.get_execution_record(execution_id, return_type)

    if record:
        if record.status == ExecutionStatus.COMPLETED:
            return record.result
        if record.status == ExecutionStatus.FAILED:
            original_error = Exception(
                record.error or "Unknown previously failed error"
            )
            raise ExecutionFailedError(
                f"Task {execution_id} failed previously and cannot be re-executed."
            ) from original_error

    async with ctx.get_transaction_scope():
        # Reset child sequencers for deterministic re-execution.
        await sequencer.reset_for_call(execution_id)

        execution = await store.start_execution(execution_id)
        tracer.start(execution_id)

        try:
            result = await func(*args, **kwargs)
            await execution.complete(result, return_type)
            return result
        except YieldException:
            # Interruption is a graceful exit; don't stage failure.
            # The state remains STARTED, allowing for resumption.
            raise
        except Exception as e:
            error_str = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            await execution.fail(error_str)
            raise ExecutionFailedError(
                f"Task {execution_id} failed: {type(e).__name__}({e})"
            ) from e
        finally:
            tracer.end()
