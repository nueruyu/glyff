"""Logical values accepted by canonical argument encoding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

CanonicalValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | list["CanonicalValue"]
    | dict[str, "CanonicalValue"]
)


@dataclass(frozen=True)
class CanonicalFallback:
    """A declared fallback representation for a canonical argument.

    The representation is what a `CanonicalFallbackRepresenter` returned, not
    the value it replaced. Canonicalizing one writes the marker again, so a
    recorded argument can pass through a canonicalizer unchanged. Passing one
    to a live call declares the representation outright, without consulting a
    fallback representer.
    """

    representation: "CanonicalArgumentValue"


CanonicalArgumentValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | CanonicalFallback
    | list["CanonicalArgumentValue"]
    | dict[str, "CanonicalArgumentValue"]
)
