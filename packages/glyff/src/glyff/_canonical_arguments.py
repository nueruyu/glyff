"""Canonical argument values and their persistent encoding."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from ._identity import ArgumentsDigest
from .exceptions import ArgumentCanonicalizationError, InvalidExecutionError

CanonicalValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | list["CanonicalValue"]
    | dict[str, "CanonicalValue"]
)

_FALLBACK_MARKER_KEY = "__glyff_opaque__"


@dataclass(frozen=True)
class CanonicalFallback:
    """A fallback representation read from canonical argument records.

    :attr:`representation` is what a `CanonicalFallbackRepresenter` returned,
    not the value it replaced — there is nothing to get back to. Canonicalizing
    one writes the marker again, so recorded arguments can pass through a
    canonicalizer unchanged.

    Passing one to a live call declares the representation outright, so no
    fallback representer is consulted for it.
    """

    representation: CanonicalValue


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
RecordedArgumentValue: TypeAlias = CanonicalArgumentValue


class CanonicalArguments:
    """Canonical argument bytes, the preimage of an execution's key.

    Not a :class:`SerializedValue`: that carries application values through a
    ``Serializer``, and only these derive an ``arguments_digest``. Stores must
    round-trip them untouched — re-encoding would change the key.
    """

    __slots__ = ("_data",)

    def __init__(self, arguments: Mapping[str, CanonicalArgumentValue]) -> None:
        self._data = encode_canonical(_encode_argument_mapping(arguments))

    @classmethod
    def _from_recorded_bytes(cls, data: bytes) -> CanonicalArguments:
        instance = cls.__new__(cls)
        instance._data = data
        return instance

    @property
    def data(self) -> bytes:
        return self._data

    def recorded(self) -> RecordedArguments:
        return RecordedArguments.from_bytes(self.data)

    @property
    def digest(self) -> ArgumentsDigest:
        return ArgumentsDigest(hashlib.sha256(self.data).hexdigest())

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CanonicalArguments) and self.data == other.data

    def __hash__(self) -> int:
        return hash(self.data)

    def __repr__(self) -> str:
        return f"CanonicalArguments(data={self.data!r})"


class RecordedArguments(Mapping[str, RecordedArgumentValue]):
    """A validated argument mapping decoded from an execution record."""

    __slots__ = ("_arguments",)

    def __init__(self, arguments: Mapping[str, RecordedArgumentValue]) -> None:
        if not all(
            isinstance(name, str) and _is_recorded_argument_value(value)
            for name, value in arguments.items()
        ):
            raise TypeError(
                "RecordedArguments requires argument names and recorded canonical values."
            )
        self._arguments = dict(arguments)

    @classmethod
    def from_bytes(cls, data: bytes) -> RecordedArguments:
        try:
            decoded = json.loads(data, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise InvalidExecutionError(
                "Recorded canonical arguments are not valid UTF-8 JSON."
            ) from error
        if not isinstance(decoded, dict) or not all(
            isinstance(name, str) for name in decoded
        ):
            raise InvalidExecutionError(
                "Recorded canonical arguments must be a mapping of argument names."
            )
        return cls(
            {name: _restore_recorded_value(value) for name, value in decoded.items()}
        )

    def __getitem__(self, name: str) -> RecordedArgumentValue:
        return self._arguments[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._arguments)

    def __len__(self) -> int:
        return len(self._arguments)


def _encode_argument_mapping(
    arguments: Mapping[str, CanonicalArgumentValue],
) -> dict[str, CanonicalValue]:
    if not all(isinstance(name, str) for name in arguments):
        raise ArgumentCanonicalizationError("Canonical argument names must be strings.")
    _require_unreserved_mapping(arguments)
    return {name: _encode_argument_value(value) for name, value in arguments.items()}


def _encode_argument_value(value: CanonicalArgumentValue) -> CanonicalValue:
    if isinstance(value, CanonicalFallback):
        if not _is_canonical_value(value.representation):
            raise ArgumentCanonicalizationError(
                "A canonical fallback representer returned a value outside the "
                "JSON data model."
            )
        return {_FALLBACK_MARKER_KEY: value.representation}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_encode_argument_value(item) for item in value]
    if isinstance(value, dict):
        return _encode_argument_mapping(value)
    raise ArgumentCanonicalizationError(
        f"Value of type '{type(value).__name__}' is not a canonical argument value."
    )


def encode_canonical(value: CanonicalValue) -> bytes:
    """Encode a canonical value with the single spelling used for identity."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
            default=_reject_uncanonical,
        ).encode("utf-8")
    except ValueError as error:
        raise ArgumentCanonicalizationError(
            "NaN and infinity are not in the JSON data model."
        ) from error


def _require_unreserved_mapping(value: Mapping[str, object]) -> None:
    if _FALLBACK_MARKER_KEY in value:
        raise ArgumentCanonicalizationError(
            "A value canonicalizing to glyff's fallback marker would collide with "
            "a fallback representation, so the key is reserved. Name the mapping key "
            "or dataclass field something else."
        )


def _restore_recorded_value(value: CanonicalValue) -> RecordedArgumentValue:
    if isinstance(value, dict):
        if len(value) == 1 and _FALLBACK_MARKER_KEY in value:
            return CanonicalFallback(value[_FALLBACK_MARKER_KEY])
        return {key: _restore_recorded_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_recorded_value(item) for item in value]
    return value


def _reject_uncanonical(value: Any) -> Any:
    raise ArgumentCanonicalizationError(
        f"Value of type '{type(value).__name__}' is not in the JSON data model, so it "
        "cannot be encoded. Canonicalize it first."
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"{value} is not in the JSON data model.")


def _is_recorded_argument_value(value: object) -> bool:
    if isinstance(value, CanonicalFallback):
        return _is_canonical_value(value.representation)
    if value is None or isinstance(value, (str, int, float, bool)):
        return not isinstance(value, float) or math.isfinite(value)
    if isinstance(value, list):
        return all(_is_recorded_argument_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_recorded_argument_value(item)
            for key, item in value.items()
        )
    return False


def _is_canonical_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return not isinstance(value, float) or math.isfinite(value)
    if isinstance(value, list):
        return all(_is_canonical_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_canonical_value(item)
            for key, item in value.items()
        )
    return False
