import functools
import inspect
from typing import Any, Callable, ParamSpec, TypeVar, cast

from ._context import Context, get_context
from ._executor import execute
from ._models import CanonicalArguments, ExecutionId
from .exceptions import MissingTypeHintError, TypeHintResolutionError
from .serialization._utils import encode_canonical

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


async def _resolve_call_identity(
    ctx: Context,
    func: Callable[..., Any],
    sig: inspect.Signature,
    task_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[ExecutionId, CanonicalArguments]:
    """Return the execution id for one call and the canonical arguments it is keyed by."""
    parent_id = ctx.current_execution_id
    canonical = ctx.argument_canonicalizer.canonicalize(func, sig, args, kwargs)
    encoded = CanonicalArguments(encode_canonical(canonical))
    seq = await ctx.sequencer.next(parent_id, task_name, encoded.digest)
    execution_id = ExecutionId(
        parent_id=parent_id,
        name=task_name,
        sequence=seq,
        arguments_digest=encoded.digest,
    )
    return execution_id, encoded


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
        execution_id, encoded_args = await _resolve_call_identity(
            ctx, func, sig, task_name, args, kwargs
        )
        result = await execute(
            ctx=ctx,
            execution_id=execution_id,
            encoded_args=encoded_args,
            func=func,
            args=args,
            kwargs=kwargs,
            return_type=return_type,
        )
        return cast(R, result)

    return cast(Callable[P, R], wrapper)
