"""Canonical argument values and their persistent encoding."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from ._types import (
    ArgumentsDigest,
    CanonicalArgumentValue,
    CanonicalFallback,
    CanonicalValue,
)
from .exceptions import ArgumentCanonicalizationError, InvalidExecutionError

_FALLBACK_MARKER_KEY = "__glyff_fallback__"


class CanonicalArguments:
    """Canonical argument bytes, the preimage of an execution's key.

    Not a :class:`SerializedValue`: that carries application values through a
    ``Serializer``, and only these derive an ``arguments_digest``. Stores must
    round-trip them untouched — re-encoding would change the key.
    """

    __slots__ = ("_data",)

    def __init__(
        self,
        arguments: Mapping[str, CanonicalArgumentValue],
    ) -> None:
        self._data = encode_canonical(_encode_argument_mapping(arguments))

    @classmethod
    def from_recorded_bytes(cls, data: bytes) -> CanonicalArguments:
        """Restore and validate canonical arguments without re-encoding them."""
        instance = cls.__new__(cls)
        instance._data = bytes(data)
        instance.decode()
        return instance

    @property
    def data(self) -> bytes:
        return self._data

    def decode(self) -> dict[str, CanonicalArgumentValue]:
        """Decode and validate the logical values stored in these arguments."""
        try:
            decoded = json.loads(self.data, parse_constant=_reject_json_constant)
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
        return {name: _restore_recorded_value(value) for name, value in decoded.items()}

    @property
    def digest(self) -> ArgumentsDigest:
        return ArgumentsDigest(hashlib.sha256(self.data).hexdigest())

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CanonicalArguments) and self.data == other.data

    def __hash__(self) -> int:
        return hash(self.data)

    def __repr__(self) -> str:
        return f"CanonicalArguments(data={self.data!r})"


def _encode_argument_mapping(
    arguments: Mapping[str, CanonicalArgumentValue],
) -> dict[str, CanonicalValue]:
    if not all(isinstance(name, str) for name in arguments):
        raise ArgumentCanonicalizationError("Canonical argument names must be strings.")
    _require_unreserved_mapping(arguments)
    return {name: _encode_argument_value(value) for name, value in arguments.items()}


def _encode_argument_value(value: CanonicalArgumentValue) -> CanonicalValue:
    if isinstance(value, CanonicalFallback):
        return {_FALLBACK_MARKER_KEY: _encode_argument_value(value.representation)}
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


def _restore_recorded_value(value: Any) -> CanonicalArgumentValue:
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
