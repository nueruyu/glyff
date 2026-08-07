from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass
from typing import Any, Callable, ParamSpec, TypeVar, cast

from ._context import Context, get_context
from ._executor import execute
from ._execution import CanonicalArguments
from ._identity import DomainId, ExecutionId, ExecutionName
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
    domain: DomainId,
    func: Callable[..., Any],
    sig: inspect.Signature,
    task_name: ExecutionName,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[ExecutionId, CanonicalArguments]:
    """Return the execution id for one call and the canonical arguments it is keyed by."""
    parent_id = ctx.current_execution_id
    canonical = ctx.argument_canonicalizer.canonicalize(func, sig, args, kwargs)
    encoded = CanonicalArguments(encode_canonical(canonical))
    seq = await ctx.sequencer.next(parent_id, domain, task_name, encoded.digest)
    execution_id = ExecutionId(
        parent_id=parent_id,
        domain=domain,
        name=task_name,
        sequence=seq,
        arguments_digest=encoded.digest,
    )
    return execution_id, encoded


@dataclass(frozen=True)
class Domain:
    """A versioned ownership boundary for engraved functions.

    A library owning engraved functions declares one, so its recorded executions
    carry its identifier and can be versioned independently of the application's
    own version. Only the identifier is part of an execution's identity; the
    version says which generation of the owner's code the records belong to.
    """

    id: DomainId
    version: str

    def __init__(self, id: DomainId | str, *, version: str) -> None:
        object.__setattr__(self, "id", DomainId(id) if isinstance(id, str) else id)
        object.__setattr__(self, "version", version)
        if not version:
            raise ValueError(f"Domain {self.id} cannot have an empty version.")

    def engrave(self, func: Callable[P, R]) -> Callable[P, R]:
        """Makes an async function engraveable and resumable within this domain.

        The domain is fixed here, at decoration: a recorded execution's owner is
        a property of the definition, not of whatever was in scope when it ran.
        """
        sig = inspect.signature(func)
        task_name = ExecutionName(getattr(func, "__qualname__", func.__name__))

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
        domain = self

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            ctx = get_context()
            # Before identity is resolved, so a recorded result is never replayed
            # against a generation of code that has not been agreed with.
            await ctx.domain_claims.ensure(domain.id, domain.version)
            execution_id, canonical_arguments = await _resolve_call_identity(
                ctx, domain.id, func, sig, task_name, args, kwargs
            )
            result = await execute(
                ctx=ctx,
                execution_id=execution_id,
                canonical_arguments=canonical_arguments,
                func=func,
                args=args,
                kwargs=kwargs,
                return_type=return_type,
            )
            return cast(R, result)

        return cast(Callable[P, R], wrapper)
