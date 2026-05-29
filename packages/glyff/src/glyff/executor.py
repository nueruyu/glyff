import inspect
import traceback
from collections.abc import AsyncIterator
from typing import Any, Callable, NoReturn

from .context import Context
from .exceptions import ExecutionFailedError, YieldException
from .models import ExecutionId, ExecutionStatus


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


async def _fail_stream(
    ctx: Context, execution_id: ExecutionId, error: Exception
) -> NoReturn:
    """Records a streaming task's failure (its own terminal write) and raises."""
    error_str = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    async with ctx.get_transaction_scope():
        execution = await ctx.store.start_execution(execution_id)
        await execution.fail(error_str)
    raise ExecutionFailedError(
        f"Task {execution_id} failed: {type(error).__name__}({error})"
    ) from error


async def _drive_producer(
    ctx: Context,
    execution_id: ExecutionId,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> AsyncIterator[Any]:
    """
    Yields items from ``func`` (an async generator function, or a coroutine
    returning an async iterator), handling the bits that must wrap the producer
    but not the consumer:

    - The tracer brackets only the advancement of the producer (each
      ``__anext__``), never the ``yield``. This keeps the shared call-stack
      balanced under out-of-LIFO consumption, parents nested ``@engrave`` calls
      made *inside* the producer to this stream (but not the consumer's own
      calls between items), and ensures an exception thrown in by the consumer
      does not reach the producer's failure handler.
    - Producer errors are recorded as failures; ``YieldException`` propagates.
    """
    tracer = ctx.tracer

    try:
        produced = func(*args, **kwargs)
        if inspect.iscoroutine(produced):
            tracer.start(execution_id)
            try:
                produced = await produced
            finally:
                tracer.end()
        iterator = aiter(produced)
    except YieldException:
        raise
    except Exception as e:
        await _fail_stream(ctx, execution_id, e)

    while True:
        try:
            tracer.start(execution_id)
            try:
                item = await anext(iterator)
            except StopAsyncIteration:
                return
            finally:
                tracer.end()
        except YieldException:
            raise
        except Exception as e:
            await _fail_stream(ctx, execution_id, e)
        yield item


async def execute_stream(
    ctx: Context,
    execution_id: ExecutionId,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    item_type: Any,
) -> AsyncIterator[Any]:
    """
    Orchestrates the execution of a streaming task (one returning an
    ``AsyncIterator``/``AsyncGenerator``).

    The whole stream is treated as a single value: items are collected while
    being transparently yielded to the caller, and only persisted as one list
    once the stream completes naturally. A completed stream is replayed from the
    store without re-running ``func``; an unfinished stream (interruption, early
    break, crash) leaves no usable record and re-runs from scratch on the next
    invocation.
    """
    store = ctx.store
    # The collected items are stored/replayed as a plain list of ``item_type``.
    list_type = list[item_type]

    record = await store.get_execution_record(execution_id, list_type)
    if record is not None:
        if record.status == ExecutionStatus.COMPLETED:
            for item in record.result or []:
                yield item
            return
        if record.status == ExecutionStatus.FAILED:
            original_error = Exception(
                record.error or "Unknown previously failed error"
            )
            raise ExecutionFailedError(
                f"Task {execution_id} failed previously and cannot be re-executed."
            ) from original_error

    # Reset child sequencers for deterministic re-execution of nested calls.
    await ctx.sequencer.reset_for_call(execution_id)

    collected: list[Any] = []
    producer = _drive_producer(ctx, execution_id, func, args, kwargs)
    try:
        async for item in producer:
            collected.append(item)
            yield item

        # Natural completion: record the full stream as one value. The scope is
        # used only for this terminal write (never held across yields), so it
        # never rolls back and never affects sibling executions.
        async with ctx.get_transaction_scope():
            execution = await store.start_execution(execution_id)
            await execution.complete(collected, list_type)
    finally:
        # Interruption (YieldException), early break / aclose (GeneratorExit) and
        # consumer-thrown exceptions all leave without recording: the partial
        # stream is discarded. Closing the producer runs its cleanup.
        await producer.aclose()
