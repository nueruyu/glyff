from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, Callable, ParamSpec, TypeVar, cast

from ._context import Context, get_context
from ._execution import CanonicalArguments
from ._executor import execute
from ._function import FunctionDefinition
from ._identity import DomainId, ExecutionId
from .exceptions import ArgumentCanonicalizationError
from .serialization._utils import encode_canonical

P = ParamSpec("P")
R = TypeVar("R")


async def _resolve_call_identity(
    ctx: Context,
    domain: DomainId,
    definition: FunctionDefinition,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[ExecutionId, CanonicalArguments]:
    """Return the execution id for one call and the canonical arguments it is keyed by."""
    parent_id = ctx.current_execution_id
    arguments = definition.bind(args, kwargs)
    try:
        canonical = ctx.argument_canonicalizer.canonicalize(arguments)
    except ArgumentCanonicalizationError as e:
        # The canonicalizer sees values, not the call they came from, so the
        # function is named here or nowhere.
        raise ArgumentCanonicalizationError(
            f"Arguments to '{definition.name}' could not be canonicalized. "
            f"Ensure all arguments have a value representation. Original error: {e}"
        ) from e
    encoded = CanonicalArguments(encode_canonical(canonical))
    seq = await ctx.sequencer.next(parent_id, domain, definition.name, encoded.digest)
    execution_id = ExecutionId(
        parent_id=parent_id,
        domain=domain,
        name=definition.name,
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
        definition = FunctionDefinition.from_callable(func)
        domain = self

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            ctx = get_context()
            # Before identity is resolved, so a recorded result is never replayed
            # against a generation of code that has not been agreed with.
            await ctx.domain_claims.ensure(domain.id, domain.version)
            execution_id, canonical_arguments = await _resolve_call_identity(
                ctx, domain.id, definition, args, kwargs
            )
            result = await execute(
                ctx=ctx,
                execution_id=execution_id,
                canonical_arguments=canonical_arguments,
                func=definition.func,
                args=args,
                kwargs=kwargs,
                return_type=definition.return_type,
            )
            return cast(R, result)

        return cast(Callable[P, R], wrapper)
