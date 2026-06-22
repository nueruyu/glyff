import functools
import inspect
from typing import Any, Callable, ParamSpec, TypeVar, cast

from ._context import Context, get_context
from .exceptions import MissingTypeHintError, TypeHintResolutionError
from ._executor import execute
from ._models import ExecutionId

P = ParamSpec("P")
R = TypeVar("R")


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
    args_hash = ctx.hasher.hash_args(func, sig, args, kwargs)
    seq = await ctx.sequencer.next(parent_id, task_name, args_hash)
    return ExecutionId(
        parent_id=parent_id, name=task_name, sequence=seq, args_hash=args_hash
    )


def engrave(func: Callable[P, R]) -> Callable[P, R]:
    """
    Decorator that makes an async method engraveable and resumable.
    Its main responsibilities are ExecutionId creation and delegation to the
    `executor` module.
    """
    sig = inspect.signature(func)
    task_name = getattr(func, "__qualname__", func.__name__)

    unevaluated_type_hints = inspect.get_annotations(func, eval_str=False)
    missing_type_hints = _missing_required_type_hints(sig, unevaluated_type_hints)
    if missing_type_hints:
        missing = ", ".join(missing_type_hints)
        raise MissingTypeHintError(
            f"Engraved function '{task_name}' is missing required type hints: "
            f"{missing}."
        )

    try:
        type_hints = inspect.get_annotations(func, eval_str=True)
    except Exception as e:
        raise TypeHintResolutionError(
            f"Could not resolve type hints for {task_name}. "
            f"Please ensure all types are correctly defined and imported. Error: {e}"
        ) from e

    return_type = type_hints["return"]

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
