import functools
import inspect
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from typing import Any, Callable, ParamSpec, TypeVar, cast, get_args, get_origin

from .context import Context, get_context
from .exceptions import MissingTypeHintError, TypeHintResolutionError
from .executor import execute, execute_stream
from .models import ExecutionId

P = ParamSpec("P")
R = TypeVar("R")

# Single source of truth for streaming return-type detection. A function is
# treated as streaming iff its return annotation (or its generic origin) is one
# of these. Custom subclasses are intentionally not picked up: replay reuses the
# generic ``list[item_type]`` for (de)serialization, and only the standard
# annotations let us extract a reliable ``item_type``.
_STREAMING_TYPES: tuple[type, ...] = (AsyncIterator, AsyncGenerator, AsyncIterable)


def _missing_required_type_hints(
    sig: inspect.Signature, type_hints: dict[str, Any]
) -> list[str]:
    missing: list[str] = []
    if "return" not in type_hints:
        missing.append("return")

    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if name not in type_hints:
            missing.append(name)

    return missing


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

    Functions whose return annotation is one of the streaming types (see
    ``_STREAMING_TYPES``) are treated as streaming: the wrapper itself becomes
    an async generator that transparently yields items while the executor
    records the full stream.
    """
    sig = inspect.signature(func)
    task_name = getattr(func, "__qualname__", func.__name__)

    try:
        type_hints = inspect.get_annotations(func, eval_str=True)
    except Exception as e:
        raise TypeHintResolutionError(
            f"Could not resolve type hints for {task_name}. "
            f"Please ensure all types are correctly defined and imported. Error: {e}"
        ) from e

    missing_type_hints = _missing_required_type_hints(sig, type_hints)
    if missing_type_hints:
        missing = ", ".join(missing_type_hints)
        raise MissingTypeHintError(
            f"Engraved function '{task_name}' is missing required type hints: "
            f"{missing}."
        )

    return_type = type_hints["return"]

    # `get_origin` unwraps subscripted generics (e.g. `AsyncIterator[T]` ->
    # `collections.abc.AsyncIterator`); bare annotations have no origin, so we
    # fall back to `return_type` itself.
    target = get_origin(return_type) or return_type
    is_streaming = target in _STREAMING_TYPES

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

    else:

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
