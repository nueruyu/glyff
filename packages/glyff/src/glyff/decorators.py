import functools
import inspect
from collections.abc import AsyncIterator
from typing import Any, Callable, ParamSpec, TypeVar, cast, get_args, get_origin

from .context import get_context
from .executor import execute
from .models import ExecutionId, ReturnTypeInfo

P = ParamSpec("P")
R = TypeVar("R")


def _analyze_return_type(func: Callable) -> ReturnTypeInfo:
    """Analyzes the function's return annotation to create a ReturnTypeInfo."""
    task_name = getattr(func, "__qualname__", func.__name__)
    try:
        type_hints = inspect.get_annotations(func, eval_str=True)
        return_type = type_hints.get("return", Any)
    except Exception as e:
        raise TypeError(
            f"Could not resolve type hints for {task_name}. "
            f"Please ensure all types are correctly defined and imported. Error: {e}"
        ) from e

    is_streaming = get_origin(return_type) is AsyncIterator
    item_type = Any
    if is_streaming:
        args = get_args(return_type)
        if args:
            item_type = args[0]

    return ReturnTypeInfo(
        full_type=return_type, is_streaming=is_streaming, item_type=item_type
    )


def engrave(func: Callable[P, R]) -> Callable[P, R]:
    """
    Decorator that makes an async method engraveable and resumable.
    Its main responsibilities are ExecutionId creation and delegation to the
    `executor` module.
    """
    sig = inspect.signature(func)
    task_name = getattr(func, "__qualname__", func.__name__)
    type_info = _analyze_return_type(func)

    if type_info.is_streaming:
        # Sync wrapper: directly returns AsyncIterator so callers need no `await`.
        @functools.wraps(func)
        def streaming_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            async def _gen() -> AsyncIterator[Any]:
                ctx = get_context()
                parent_id = ctx.current_execution_id
                seq = await ctx.sequencer.next(parent_id, task_name)
                args_hash = ctx.hasher.hash_args(func, sig, args, kwargs)
                execution_id = ExecutionId(
                    parent_id=parent_id,
                    name=task_name,
                    sequence=seq,
                    args_hash=args_hash,
                )
                stream = await execute(
                    ctx=ctx,
                    execution_id=execution_id,
                    func=func,
                    args=args,
                    kwargs=kwargs,
                    type_info=type_info,
                )
                async for item in stream:
                    yield item

            return cast(R, _gen())

        return cast(Callable[P, R], streaming_wrapper)
    else:

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            ctx = get_context()
            parent_id = ctx.current_execution_id
            seq = await ctx.sequencer.next(parent_id, task_name)
            args_hash = ctx.hasher.hash_args(func, sig, args, kwargs)
            execution_id = ExecutionId(
                parent_id=parent_id, name=task_name, sequence=seq, args_hash=args_hash
            )
            result = await execute(
                ctx=ctx,
                execution_id=execution_id,
                func=func,
                args=args,
                kwargs=kwargs,
                type_info=type_info,
            )
            return cast(R, result)

        return cast(Callable[P, R], wrapper)
