from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TypeAlias

from .exceptions import InvalidExecutionError

# A domain id outlives the code that declared it, so it is kept to a character
# set that reads the same everywhere.
_DOMAIN_ID = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*")

_EXPLICIT_EXECUTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class DomainId:
    """A domain's persistent machine identifier."""

    value: str

    def __post_init__(self) -> None:
        if not _DOMAIN_ID.fullmatch(self.value):
            raise ValueError(
                f"{self.value!r} is not a valid domain id: expected lowercase "
                "ASCII segments of letters, digits, underscores and hyphens, "
                "joined by dots, each starting with a letter or digit."
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ExecutionName:
    """The name an engraved function is recorded under.

    Deliberately permissive: today's inferred names come from ``__qualname__``
    and look like ``outer.<locals>.task``, and a migration has to be able to
    hold whatever a previous version wrote. Names a caller *declares* go through
    :meth:`explicit`, which is where a grammar can be insisted on.
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("An execution name cannot be empty.")

    @classmethod
    def explicit(cls, value: str) -> ExecutionName:
        """A name chosen by the caller, held to the declared-name grammar."""
        if not _EXPLICIT_EXECUTION_NAME.fullmatch(value):
            raise ValueError(
                f"{value!r} is not a valid explicit execution name: expected "
                "letters, digits, dots, underscores and hyphens, starting with "
                "a letter or digit."
            )
        return cls(value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ArgumentsDigest:
    """A digest over canonical arguments.

    Opaque: glyff neither reads nor constrains its characters, so a store must
    round-trip whatever it is handed.
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("An arguments digest cannot be empty.")

    def __str__(self) -> str:
        return self.value


# A value in the JSON data model. Canonicalizing a call's arguments produces one of
# these, and a migration's argument conversion both receives and returns one.
CanonicalValue: TypeAlias = (
    "str | int | float | bool | None | list[CanonicalValue] | dict[str, CanonicalValue]"
)


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


@dataclass(frozen=True)
class SessionId:
    """A non-empty, application-defined session identifier."""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("A session id cannot be empty.")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ExecutionId:
    """
    A unique, deterministic identifier for a task call.
    It forms a hierarchy through the 'parent_id' attribute.
    """

    parent_id: ExecutionId | None
    domain: DomainId
    name: ExecutionName
    sequence: int
    arguments_digest: ArgumentsDigest

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
        return (
            f"ExecutionId(domain='{self.domain}', name='{self.name}', "
            f"sequence={self.sequence}{parent_info})"
        )


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
