"""The recorded arguments a migration reads, and what it hands back.

A record keeps the canonical form of a call's arguments, which is plain JSON
except where an `OpaquePolicy` stood in for a value. That marker is glyff's own,
so it is given a name on the way in and put back on the way out rather than
appearing in the migrations people write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .._execution import CanonicalValue
from ..serialization._utils import OPAQUE_TAG


@dataclass(frozen=True)
class Opaque:
    """A recorded argument that stands for a value, not the value itself.

    An `OpaquePolicy` decided how a value with no value representation appears in
    a key; :attr:`value` is that representation. Returning one unchanged keeps
    the argument exactly as it was recorded.
    """

    value: CanonicalValue


def from_recorded(value: CanonicalValue) -> Any:
    """Recorded canonical arguments, with glyff's own marker given a name."""
    if isinstance(value, dict):
        if set(value) == {OPAQUE_TAG}:
            return Opaque(value[OPAQUE_TAG])
        return {key: from_recorded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [from_recorded(item) for item in value]
    return value


def restore(value: Any) -> Any:
    """Puts the marker back, so a canonicalizer is handed what it wrote.

    Canonicalizing is idempotent on its own output, so a marker that survives
    this substitution keys the call exactly as the recorded one did.
    """
    if isinstance(value, Opaque):
        return {OPAQUE_TAG: value.value}
    if isinstance(value, dict):
        return {key: restore(item) for key, item in value.items()}
    if isinstance(value, list):
        return [restore(item) for item in value]
    return value
