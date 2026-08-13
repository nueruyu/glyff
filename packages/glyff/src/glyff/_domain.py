from __future__ import annotations

import functools
from typing import Any, Callable, ParamSpec, TypeVar, cast, overload

from ._context import Context, get_context
from ._canonical_arguments import CanonicalArguments
from ._executor import execute
from ._function import FunctionDefinition
from ._types import DomainId, DomainVersion, ExecutionId, ExecutionName
from .exceptions import ArgumentCanonicalizationError

P = ParamSpec("P")
R = TypeVar("R")


async def _resolve_call_identity(
    ctx: Context,
    domain_id: DomainId,
    definition: FunctionDefinition,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[ExecutionId, CanonicalArguments]:
    """Return the execution id for one call and the canonical arguments it is keyed by."""
    parent_id = ctx.current_execution_id
    arguments = definition.bind(args, kwargs)
    try:
        canonical_arguments = ctx.argument_canonicalizer.canonicalize(arguments)
    except ArgumentCanonicalizationError as e:
        # The canonicalizer sees values, not the call they came from, so the
        # function is named here or nowhere.
        raise ArgumentCanonicalizationError(
            f"Arguments to '{definition.name}' could not be canonicalized. "
            f"Ensure all arguments have a value representation. Original error: {e}"
        ) from e
    seq = await ctx.sequencer.next(
        parent_id, domain_id, definition.name, canonical_arguments.digest
    )
    execution_id = ExecutionId(
        parent_id=parent_id,
        domain_id=domain_id,
        name=definition.name,
        sequence=seq,
        arguments_digest=canonical_arguments.digest,
    )
    return execution_id, canonical_arguments


class Domain:
    """A versioned ownership boundary for engraved functions.

    A library owning engraved functions declares one, so its recorded executions
    carry its identifier and can be versioned independently of the application's
    own version. Only the identifier is part of an execution's identity; the
    version says which generation of the owner's code the records belong to.
    """

    __slots__ = ("_id", "_version")

    def __init__(self, id: DomainId | str, *, version: DomainVersion | str) -> None:
        self._id = id if isinstance(id, DomainId) else DomainId(id)
        self._version = (
            version if isinstance(version, DomainVersion) else DomainVersion(version)
        )

    @property
    def id(self) -> DomainId:
        return self._id

    @property
    def version(self) -> DomainVersion:
        return self._version

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Domain)
            and self.id == other.id
            and self.version == other.version
        )

    def __repr__(self) -> str:
        return f"Domain(id={self.id!r}, version={self.version!r})"

    @overload
    def engrave(
        self,
        func: Callable[P, R],
        /,
        *,
        name: str | None = None,
    ) -> Callable[P, R]: ...

    @overload
    def engrave(
        self,
        func: None = None,
        /,
        *,
        name: str | None = None,
    ) -> Callable[[Callable[P, R]], Callable[P, R]]: ...

    def engrave(
        self,
        func: Callable[P, R] | None = None,
        /,
        *,
        name: str | None = None,
    ) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
        """Makes an async function engraveable and resumable within this domain.

        The domain is fixed here, at decoration: a recorded execution's owner is
        a property of the definition, not of whatever was in scope when it ran.
        """
        explicit_name = ExecutionName.explicit(name) if name is not None else None
        if func is None:
            return lambda target: self._engrave(target, explicit_name)
        return self._engrave(func, explicit_name)

    def _engrave(
        self,
        func: Callable[P, R],
        name: ExecutionName | None,
    ) -> Callable[P, R]:
        definition = FunctionDefinition.from_callable(func, name=name)
        domain_id = self.id
        domain_version = self.version

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            ctx = get_context()
            # Before identity is resolved, so a recorded result is never replayed
            # against a generation of code that has not been agreed with.
            await ctx.domain_claims.ensure(domain_id, domain_version)
            execution_id, canonical_arguments = await _resolve_call_identity(
                ctx, domain_id, definition, args, kwargs
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
