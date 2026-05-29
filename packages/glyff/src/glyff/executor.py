import inspect
import traceback
from collections.abc import AsyncIterator
from typing import Any, Callable, NoReturn

from .context import Context
from .exceptions import ExecutionFailedError, YieldException
from .models import ExecutionId, ExecutionRecord, ExecutionStatus

# Sentinel returned by `_advance_stream` when the producer is exhausted. A unique
# object so it never collides with a legitimately yielded value.
_STREAM_EXHAUSTED: Any = object()


def _format_error(e: Exception) -> str:
    return "".join(traceback.format_exception(type(e), e, e.__traceback__))


def _task_failed_error(execution_id: ExecutionId, e: Exception) -> ExecutionFailedError:
    return ExecutionFailedError(
        f"Task {execution_id} failed: {type(e).__name__}({e})"
    )


def _raise_previously_failed(
    execution_id: ExecutionId, record: ExecutionRecord
) -> NoReturn:
    raise ExecutionFailedError(
        f"Task {execution_id} failed previously and cannot be re-executed."
    ) from Exception(record.error or "Unknown previously failed error")


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
            _raise_previously_failed(execution_id, record)

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
            raise _task_failed_error(execution_id, e) from e
        finally:
            tracer.end()


async def _replay_stream(
    ctx: Context, execution_id: ExecutionId, list_type: type
) -> list[Any] | None:
    """
    Returns the stored item list for a completed stream, or ``None`` if the
    stream should run (no record, or a re-runnable ``STARTED`` record). Raises
    ``ExecutionFailedError`` if the stream previously failed.
    """
    record = await ctx.store.get_execution_record(execution_id, list_type)
    if record is None:
        return None
    if record.status == ExecutionStatus.COMPLETED:
        return record.result or []
    if record.status == ExecutionStatus.FAILED:
        _raise_previously_failed(execution_id, record)
    return None  # STARTED → re-run from scratch


async def _open_stream_iterator(
    ctx: Context,
    execution_id: ExecutionId,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> AsyncIterator[Any]:
    """
    Calls ``func`` and normalises its result to a single async iterator. ``func``
    may be an async generator function, or a coroutine returning an async
    iterator. Errors raised while producing the iterator are recorded as
    failures; ``YieldException`` propagates untouched.
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
        return aiter(produced)
    except YieldException:
        raise
    except Exception as e:
        await _record_stream_failure(ctx, execution_id, e)
        raise _task_failed_error(execution_id, e) from e


async def _advance_stream(
    ctx: Context, execution_id: ExecutionId, iterator: AsyncIterator[Any]
) -> Any:
    """
    Pulls the next item from the producer, returning ``_STREAM_EXHAUSTED`` when
    it is done.

    The tracer brackets only this advancement (never the caller's consumption),
    so nested ``@engrave`` calls made inside the producer are parented to this
    stream while the caller's calls between items are not. Producer errors are
    recorded as failures; ``YieldException`` propagates untouched.
    """
    tracer = ctx.tracer
    try:
        tracer.start(execution_id)
        try:
            return await anext(iterator)
        except StopAsyncIteration:
            return _STREAM_EXHAUSTED
        finally:
            tracer.end()
    except YieldException:
        raise
    except Exception as e:
        await _record_stream_failure(ctx, execution_id, e)
        raise _task_failed_error(execution_id, e) from e


async def _record_stream_completion(
    ctx: Context, execution_id: ExecutionId, collected: list[Any], list_type: type
) -> None:
    """Durably records the full stream as a single value in its terminal write."""
    async with ctx.get_transaction_scope():
        execution = await ctx.store.start_execution(execution_id)
        await execution.complete(collected, list_type)


async def _record_stream_failure(
    ctx: Context, execution_id: ExecutionId, error: Exception
) -> None:
    """Durably records a streaming task's failure in its own terminal write."""
    async with ctx.get_transaction_scope():
        execution = await ctx.store.start_execution(execution_id)
        await execution.fail(_format_error(error))


async def _aclose_quietly(iterator: AsyncIterator[Any] | None) -> None:
    """Closes the underlying iterator (if closeable) so its cleanup runs."""
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        await aclose()


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
    list_type = list[item_type]

    replay = await _replay_stream(ctx, execution_id, list_type)
    if replay is not None:
        for item in replay:
            yield item
        return

    # Reset child sequencers for deterministic re-execution of nested calls.
    await ctx.sequencer.reset_for_call(execution_id)

    iterator = await _open_stream_iterator(ctx, execution_id, func, args, kwargs)
    collected: list[Any] = []
    try:
        while True:
            item = await _advance_stream(ctx, execution_id, iterator)
            if item is _STREAM_EXHAUSTED:
                break
            collected.append(item)
            yield item

        await _record_stream_completion(ctx, execution_id, collected, list_type)
    finally:
        # Interruption (YieldException), early break / aclose (GeneratorExit), and
        # caller-thrown exceptions all leave without recording: the partial stream
        # is discarded. Close the underlying iterator so its cleanup runs.
        await _aclose_quietly(iterator)
