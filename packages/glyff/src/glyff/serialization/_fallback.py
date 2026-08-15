from abc import ABC, abstractmethod
from typing import Any, cast

from .._types import CanonicalArgumentValue
from ..exceptions import ArgumentCanonicalizationError


class CanonicalFallbackRepresenter(ABC):
    """Represents values unsupported by the standard canonicalization rules."""

    @abstractmethod
    def represent(self, value: Any) -> CanonicalArgumentValue:
        """Return the value's canonical fallback representation."""
        ...


class FallbackByTypeQualname(CanonicalFallbackRepresenter):
    """Represents every instance of a type by that type's qualified name.

    Correct only when instances carry no state that should distinguish calls.
    """

    def represent(self, value: Any) -> CanonicalArgumentValue:
        value_type = cast(type[Any], type(value))
        return f"{value_type.__module__}.{value_type.__qualname__}"


class _RejectFallback(CanonicalFallbackRepresenter):
    def represent(self, value: Any) -> CanonicalArgumentValue:
        raise ArgumentCanonicalizationError(
            f"Cannot canonicalize value of type '{type(value).__name__}': it has "
            "no canonical representation. Give it a supported value representation "
            "or pass a fallback representer to the canonicalizer."
        )


_REJECT_FALLBACK = _RejectFallback()


def fallback_representer_or_reject(
    representer: CanonicalFallbackRepresenter | None,
) -> CanonicalFallbackRepresenter:
    return _REJECT_FALLBACK if representer is None else representer
