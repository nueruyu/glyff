"""What glyff understands an engraved Python function to be.

The one place that reads Python's reflection API. Everything downstream — the
identity of a call, the canonical form of its arguments — speaks in names and
values, so the mechanics of signatures, annotations and binding stop here.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from ._types import ExecutionName
from .exceptions import MissingTypeHintError, TypeHintResolutionError


@dataclass(frozen=True)
class FunctionDefinition:
    """An engraved function, reduced to what glyff needs of it."""

    func: Callable[..., Any]
    name: ExecutionName
    return_type: Any
    _signature: inspect.Signature = field(repr=False)

    @classmethod
    def from_callable(cls, func: Callable[..., Any]) -> FunctionDefinition:
        """Reads a function, refusing one glyff could not record faithfully.

        Both hint checks run at decoration, where the failure names a definition
        rather than a call.
        """
        signature = inspect.signature(func)
        name = ExecutionName(getattr(func, "__qualname__", func.__name__))

        # Unevaluated first: a hint that is merely absent is a different failure
        # from one that cannot be resolved, and only this pass distinguishes them.
        missing = _missing_required_type_hints(
            signature, inspect.get_annotations(func, eval_str=False)
        )
        if missing:
            named = ", ".join(missing)
            raise MissingTypeHintError(
                f"Engraved function '{name}' is missing required type hints: {named}."
            )

        try:
            type_hints = inspect.get_annotations(func, eval_str=True)
        except Exception as e:
            raise TypeHintResolutionError(
                f"Could not resolve type hints for {name}. "
                f"Please ensure all types are correctly defined and imported. Error: {e}"
            ) from e

        return cls(
            func=func,
            name=name,
            return_type=type_hints["return"],
            _signature=signature,
        )

    def bind(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        """Resolves one call into the name-to-value mapping it is keyed by.

        Defaults are applied, so calling with and without an argument that has
        one is the same call. Variadic parameters arrive as a tuple and a dict,
        both of which canonicalize, so they contribute to identity.
        """
        bound = self._signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)


def _missing_required_type_hints(
    signature: inspect.Signature, type_hints: dict[str, Any]
) -> list[str]:
    missing: list[str] = []
    if "return" not in type_hints:
        missing.append("return")

    for name, parameter in signature.parameters.items():
        if name in ("self", "cls"):
            continue
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if name not in type_hints:
            missing.append(name)

    return missing
