import functools
import inspect
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, Callable, ParamSpec, TypeVar, cast, get_args, get_origin

from .context import Context, get_context
from .executor import execute, execute_stream
from .models import ExecutionId

P = ParamSpec("P")
R = TypeVar("R")


async def _resolve_execution_id(
    ctx: Context,
    func: Callable[..., Any],
    sig: inspect.Signature,
    task_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> ExecutionId:
    """Builds the deterministic ExecutionId for a single call."""
    parent_id = ctx.current_execution_id
    seq = await ctx.sequencer.next(parent_id, task_name)
    args_hash = ctx.hasher.hash_args(func, sig, args, kwargs)
    return ExecutionId(
        parent_id=parent_id, name=task_name, sequence=seq, args_hash=args_hash
    )


def engrave(func: Callable[P, R]) -> Callable[P, R]:
    """
    Decorator that makes an async method engraveable and resumable.
    Its main responsibilities are ExecutionId creation and delegation to the
    `executor` module.

    Functions annotated to return an ``AsyncIterator``/``AsyncGenerator`` are
    treated as streaming: the wrapper itself becomes an async generator that
    transparently yields items while the executor records the full stream.
    """
    sig = inspect.signature(func)
    task_name = getattr(func, "__qualname__", func.__name__)

    try:
        type_hints = inspect.get_annotations(func, eval_str=True)
        return_type = type_hints.get("return", Any)
    except Exception as e:
        raise TypeError(
            f"Could not resolve type hints for {task_name}. "
            f"Please ensure all types are correctly defined and imported. Error: {e}"
        ) from e

    # Detect streaming return types. Note `get_origin(AsyncGenerator[T, None])`
    # is `collections.abc.AsyncGenerator`, distinct from `AsyncIterator`, so both
    # origins must be checked. A bare (unsubscripted) annotation has no origin,
    # so it is matched directly.
    origin = get_origin(return_type)
    is_streaming = origin in (AsyncIterator, AsyncGenerator) or return_type in (
        AsyncIterator,
        AsyncGenerator,
    )

    if is_streaming:
        item_type: Any = Any
        type_args = get_args(return_type)
        if type_args:
            item_type = type_args[0]

        @functools.wraps(func)
        async def streaming_wrapper(
            *args: P.args, **kwargs: P.kwargs
        ) -> AsyncIterator[Any]:
            ctx = get_context()
            execution_id = await _resolve_execution_id(
                ctx, func, sig, task_name, args, kwargs
            )
            async for item in execute_stream(
                ctx=ctx,
                execution_id=execution_id,
                func=func,
                args=args,
                kwargs=kwargs,
                item_type=item_type,
            ):
                yield item

        return cast(Callable[P, R], streaming_wrapper)

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        ctx = get_context()
        execution_id = await _resolve_execution_id(
            ctx, func, sig, task_name, args, kwargs
        )
        result = await execute(
            ctx=ctx,
            execution_id=execution_id,
            func=func,
            args=args,
            kwargs=kwargs,
            return_type=return_type,
        )
        return cast(R, result)

    return cast(Callable[P, R], wrapper)
