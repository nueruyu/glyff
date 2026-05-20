import functools
import inspect
from typing import Any, Callable, ParamSpec, TypeVar, cast

from .context import get_context
from .executor import execute
from .models import ExecutionId

P = ParamSpec("P")
R = TypeVar("R")


def engrave(func: Callable[P, R]) -> Callable[P, R]:
    """
    Decorator that makes an async method engraveable and resumable.
    Its main responsibilities are ExecutionId creation and delegation to the
    `executor` module.
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
            return_type=return_type,
        )
        return cast(R, result)

    return cast(Callable[P, R], wrapper)
