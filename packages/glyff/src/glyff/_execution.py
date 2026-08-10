"""The aggregate a recorded execution is, and the values it holds."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TypeAlias

from ._identity import ArgumentsDigest, ExecutionId
from .exceptions import InvalidExecutionError

# A value in the JSON data model. Canonicalizing a call's arguments produces one of
# these, and a migration's argument conversion both receives and returns one.
CanonicalValue: TypeAlias = (
    "str | int | float | bool | None | list[CanonicalValue] | dict[str, CanonicalValue]"
)

# A whole call's arguments, canonicalized. Always a mapping, because it is keyed
# by the names the call was bound to.
CanonicalArgumentMap: TypeAlias = "dict[str, CanonicalValue]"

# Reserved marker for opaque canonical values.
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


def opaque_marker(representation: CanonicalValue) -> CanonicalValue:
    """The canonical form an opaque value is recorded as."""
    return {_OPAQUE_MARKER_KEY: representation}


def is_opaque_marker(value: CanonicalValue) -> bool:
    """Whether a canonical value is what :func:`opaque_marker` writes."""
    return isinstance(value, dict) and len(value) == 1 and _OPAQUE_MARKER_KEY in value


def claims_opaque_marker(value: CanonicalValue) -> bool:
    """Whether a mapping claims the reserved key, however it came to."""
    return isinstance(value, dict) and _OPAQUE_MARKER_KEY in value


def opaque_marker_representation(value: CanonicalValue) -> CanonicalValue:
    """What the policy returned, from a value :func:`is_opaque_marker` accepts."""
    assert isinstance(value, dict)
    return value[_OPAQUE_MARKER_KEY]


@dataclass(frozen=True)
class CanonicalArguments:
    """Canonical argument bytes, the preimage of an execution's key.

    Not a :class:`SerializedValue`: that carries application values through a
    ``Serializer``, and only these derive an ``arguments_digest``. Stores must round-trip
    them untouched — re-encoding would change the key.
    """

    data: bytes

    @property
    def digest(self) -> ArgumentsDigest:
        return ArgumentsDigest(hashlib.sha256(self.data).hexdigest())


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
