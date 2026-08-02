from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TypeAlias

# A value in the JSON data model. Canonicalizing a call's arguments produces one of
# these, and a migration's argument conversion both receives and returns one.
CanonicalValue: TypeAlias = (
    "str | int | float | bool | None | list[CanonicalValue] | dict[str, CanonicalValue]"
)


@dataclass(frozen=True)
class EncodedArguments:
    """A call's canonical arguments, encoded.

    Distinct from :class:`SerializedValue`: that carries application values through
    a ``Serializer``, while these come from the canonicalization path and are the
    preimage of the execution's key.
    """

    data: bytes

    @property
    def digest(self) -> str:
        """The ``args_hash`` of an execution keyed by these arguments."""
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class ExecutionId:
    """
    A unique, deterministic identifier for a task call.
    It forms a hierarchy through the 'parent_id' attribute.
    """

    parent_id: ExecutionId | None
    name: str
    sequence: int
    args_hash: str

    def __str__(self) -> str:
        """
        Generates a human-readable representation for debugging purposes only.
        This format is NOT guaranteed to be stable or suitable for use as a persistence key.
        """
        parent_info = (
            f", parent='{self.parent_id.name}#{self.parent_id.sequence}'"
            if self.parent_id
            else ""
        )
        return f"ExecutionId(name='{self.name}', sequence={self.sequence}{parent_info})"


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
    """Child entity/value object inside the Execution aggregate."""

    key: str
    value: SerializedValue


@dataclass
class Execution:
    """Aggregate Root for a single task execution."""

    id: ExecutionId
    status: ExecutionStatus
    args: EncodedArguments
    """The canonical arguments this call was keyed by.

    Invariant, enforced on construction and relied on by migration:

        id.args_hash == args.digest

    Stores must therefore round-trip these bytes untouched; re-encoding them would
    break the key.
    """
    result: SerializedValue | None = None
    metadata: dict[str, Metadata] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.id.args_hash != self.args.digest:
            raise ValueError(
                f"Execution {self.id} does not match its recorded arguments: "
                "args_hash must be the digest of args."
            )

    @classmethod
    def start(cls, execution_id: ExecutionId, args: EncodedArguments) -> "Execution":
        return cls(id=execution_id, status=ExecutionStatus.STARTED, args=args)

    def complete(self, result: SerializedValue) -> None:
        if self.status is ExecutionStatus.COMPLETED:
            raise ValueError(f"Cannot complete execution {self.id}: already completed")
        self.status = ExecutionStatus.COMPLETED
        self.result = result

    def set_metadata(self, key: str, value: SerializedValue) -> None:
        self.metadata[key] = Metadata(key=key, value=value)

    def get_metadata(self, key: str) -> Metadata | None:
        return self.metadata.get(key)
