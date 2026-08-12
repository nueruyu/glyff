"""The aggregate a recorded execution is, and the values it holds."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, TypeAlias

from ._identity import ArgumentsDigest, ExecutionId
from .exceptions import ArgumentCanonicalizationError, InvalidExecutionError

CanonicalValue: TypeAlias = (
    "str | int | float | bool | None | list[CanonicalValue] | dict[str, CanonicalValue]"
)

CanonicalArgumentMap: TypeAlias = "dict[str, CanonicalValue]"

RecordedArgumentValue: TypeAlias = "str | int | float | bool | None | Opaque | list[RecordedArgumentValue] | dict[str, RecordedArgumentValue]"  # noqa: E501

_OPAQUE_MARKER_KEY = "__glyff_opaque__"


@dataclass(frozen=True)
class Opaque:
    """A canonical value standing in for one with no value representation.

    :attr:`representation` is what an `OpaquePolicy` returned, not the value it
    replaced — there is nothing to get back to. Canonicalizing one writes the
    marker again, which is how a recorded argument goes back through a
    canonicalizer without a mapping being able to pass itself off as one.

    Passing one to a live call is a deliberate escape hatch: it declares the
    representation outright, so no policy is consulted for it.
    """

    representation: CanonicalValue


def make_opaque_marker(representation: CanonicalValue) -> CanonicalValue:
    """The canonical form an opaque value is recorded as."""
    return {_OPAQUE_MARKER_KEY: representation}


def require_unreserved_canonical_mapping(value: CanonicalValue) -> None:
    """Refuses a canonical mapping that claims the marker's key as its own.

    A canonicalizer owes this to every mapping it derives from a value: one
    reaching the key by another route would share an opaque value's key, and
    read back as one.
    """
    if isinstance(value, dict) and _OPAQUE_MARKER_KEY in value:
        raise ArgumentCanonicalizationError(
            "A value canonicalizing to glyff's opaque marker would collide with "
            "an opaque value's key, so the key is reserved. Name the mapping key "
            "or dataclass field something else."
        )


def restore_recorded_canonical_value(
    value: CanonicalValue,
) -> RecordedArgumentValue:
    """Reads a recorded canonical value back, markers and all.

    A marker becomes an `Opaque` wherever it sits, which is what makes the form
    canonicalize to itself again: handed back as it came, it writes the same
    bytes, and a mapping cannot pass itself off as one on the way.
    """
    if isinstance(value, dict):
        if len(value) == 1 and _OPAQUE_MARKER_KEY in value:
            return Opaque(value[_OPAQUE_MARKER_KEY])
        return {
            key: restore_recorded_canonical_value(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [restore_recorded_canonical_value(item) for item in value]
    return value


@dataclass(frozen=True)
class CanonicalArguments:
    """Canonical argument bytes, the preimage of an execution's key.

    Not a :class:`SerializedValue`: that carries application values through a
    ``Serializer``, and only these derive an ``arguments_digest``. Stores must round-trip
    them untouched — re-encoding would change the key.
    """

    data: bytes

    @classmethod
    def from_canonical(cls, arguments: CanonicalArgumentMap) -> CanonicalArguments:
        """Encodes a canonical argument mapping into the bytes it is keyed by."""
        return cls(encode_canonical(arguments))

    def recorded(self) -> RecordedArguments:
        return RecordedArguments.from_bytes(self.data)

    @property
    def digest(self) -> ArgumentsDigest:
        return ArgumentsDigest(hashlib.sha256(self.data).hexdigest())


def encode_canonical(value: CanonicalValue) -> bytes:
    """The single encoder for argument identity.

    Compact and key-sorted, so one canonical form has one spelling. Its options
    are fixed here rather than shared with the serializers': what a store may
    reformat, a key may not.
    """
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


def _reject_uncanonical(value: Any) -> Any:
    raise ArgumentCanonicalizationError(
        f"Value of type '{type(value).__name__}' is not in the JSON data model, so it "
        "cannot be encoded. Canonicalize it first."
    )


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
            {
                name: restore_recorded_canonical_value(value)
                for name, value in decoded.items()
            }
        )

    def __getitem__(self, name: str) -> RecordedArgumentValue:
        return self._arguments[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._arguments)

    def __len__(self) -> int:
        return len(self._arguments)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"{value} is not in the JSON data model.")


def _is_recorded_argument_value(value: object) -> bool:
    if isinstance(value, Opaque):
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


class ExecutionStatus(Enum):
    """Represents the lifecycle state of a task execution."""

    STARTED = auto()
    COMPLETED = auto()


@dataclass(frozen=True)
class SerializedValue:
    """A serializer-neutral persisted value owned by an Execution aggregate."""

    data: bytes


@dataclass(frozen=True)
class Metadata:
    """A named serialized entry owned by an Execution."""

    key: str
    value: SerializedValue


@dataclass
class Execution:
    """A recorded task execution."""

    id: ExecutionId
    status: ExecutionStatus
    arguments: CanonicalArguments
    """The arguments this call was keyed by: ``id.arguments_digest == arguments.digest``."""
    result: SerializedValue | None = None
    metadata: dict[str, Metadata] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.id.arguments_digest != self.arguments.digest:
            raise InvalidExecutionError(
                f"Execution {self.id} does not match its recorded arguments: "
                "arguments_digest must be the digest of arguments."
            )
        completed = self.status is ExecutionStatus.COMPLETED
        if completed and self.result is None:
            raise InvalidExecutionError(f"Completed execution {self.id} has no result.")
        if not completed and self.result is not None:
            raise InvalidExecutionError(
                f"Execution {self.id} carries a result but is not completed."
            )

    @classmethod
    def start(
        cls, execution_id: ExecutionId, arguments: CanonicalArguments
    ) -> "Execution":
        return cls(id=execution_id, status=ExecutionStatus.STARTED, arguments=arguments)

    def complete(self, result: SerializedValue) -> None:
        if self.status is ExecutionStatus.COMPLETED:
            raise ValueError(f"Cannot complete execution {self.id}: already completed")
        self.status = ExecutionStatus.COMPLETED
        self.result = result

    def set_metadata(self, key: str, value: SerializedValue) -> None:
        self.metadata[key] = Metadata(key=key, value=value)

    def get_metadata(self, key: str) -> Metadata | None:
        return self.metadata.get(key)
