import inspect
import traceback
from collections.abc import AsyncIterator
from typing import Any, Callable

from .context import Context
from .exceptions import ExecutionFailedError, YieldException
from .models import ExecutionId, ExecutionStatus


def _format_error(e: Exception) -> str:
    return "".join(traceback.format_exception(type(e), e, e.__traceback__))


async def _record_stream_failure(
    ctx: Context, execution_id: ExecutionId, error: Exception
) -> None:
    """Durably records a streaming task's failure in its own terminal write."""
    async with ctx.get_transaction_scope():
        execution = await ctx.store.start_execution(execution_id)
        await execution.fail(_format_error(error))


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
            await execution.fail(_format_error(e))
            raise ExecutionFailedError(
                f"Task {execution_id} failed: {type(e).__name__}({e})"
            ) from e
        finally:
            tracer.end()


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

    ``tracer.start``/``tracer.end`` bracket only the advancement of ``func``
    (each ``__anext__``), never the ``yield`` back to the caller. This keeps the
    shared call-stack balanced even when streams are consumed out of LIFO order,
    and ensures nested ``@engrave`` calls made *inside* ``func`` are parented to
    this stream while the caller's own calls between items are not.
    """
    store = ctx.store
    sequencer = ctx.sequencer
    tracer = ctx.tracer

    # The collected items are stored/replayed as a plain list of ``item_type``.
    list_type = list[item_type]

    record = await store.get_execution_record(execution_id, list_type)

    if record:
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
    await sequencer.reset_for_call(execution_id)

    collected: list[Any] = []
    iterator: Any = None
    try:
        # Build the iterator. ``func`` may be an async generator function, or a
        # coroutine returning an async iterator. Errors here are producer errors.
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
            await _record_stream_failure(ctx, execution_id, e)
            raise ExecutionFailedError(
                f"Task {execution_id} failed: {type(e).__name__}({e})"
            ) from e

        while True:
            # Advance the producer. Producer errors are recorded as failures.
            # The ``yield`` below is deliberately *outside* this guard, so an
            # exception thrown in by the caller (e.g. via ``athrow``) propagates
            # without marking the stream failed.
            try:
                tracer.start(execution_id)
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    break
                finally:
                    # Pop before yielding control so the caller's own calls
                    # between items are not parented to this stream.
                    tracer.end()
            except YieldException:
                # Interruption is a graceful exit; nothing is recorded, so the
                # stream re-runs from scratch when the session is resumed.
                raise
            except Exception as e:
                await _record_stream_failure(ctx, execution_id, e)
                raise ExecutionFailedError(
                    f"Task {execution_id} failed: {type(e).__name__}({e})"
                ) from e

            collected.append(item)
            yield item

        # Natural completion: durably record the full result. The transaction
        # scope is used only for this terminal write (never held across yields),
        # so it never rolls back and never affects sibling executions.
        async with ctx.get_transaction_scope():
            execution = await store.start_execution(execution_id)
            await execution.complete(collected, list_type)
    finally:
        # Interruption (YieldException), early break / aclose (GeneratorExit) and
        # caller-thrown exceptions all fall through here without recording: the
        # partial stream is simply discarded. Close the underlying iterator so
        # its own cleanup runs.
        if iterator is not None:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                await aclose()
