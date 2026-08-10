"""The recorded arguments a migration reads, and what it hands back."""

from __future__ import annotations

from typing import TypeAlias

from .._execution import (
    CanonicalValue,
    Opaque,
    is_opaque_marker,
    opaque_marker_representation,
)

RecordedArgumentValue: TypeAlias = "str | int | float | bool | None | Opaque | list[RecordedArgumentValue] | dict[str, RecordedArgumentValue]"  # noqa: E501
"""One recorded argument value, with the markers glyff wrote given a name."""


def from_recorded(value: CanonicalValue) -> RecordedArgumentValue:
    """Reads recorded canonical arguments into what a conversion is handed.

    Markers become `Opaque` wherever they sit, so a conversion can hand any of
    them back and have the canonicalizer write them out as they were.
    """
    if is_opaque_marker(value):
        return Opaque(opaque_marker_representation(value))
    if isinstance(value, dict):
        return {key: from_recorded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [from_recorded(item) for item in value]
    return value
