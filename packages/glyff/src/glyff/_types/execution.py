"""Value objects composing a recorded execution identity."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .domain import DomainId

_EXPLICIT_EXECUTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class ExecutionName:
    """The name an engraved function is recorded under.

    Inferred names may contain Python qualified-name syntax. Names chosen by a
    caller go through :meth:`explicit`, which applies a narrower grammar.
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
    """A digest over canonical arguments, left uninterpreted by glyff."""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("An arguments digest cannot be empty.")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ExecutionId:
    """A unique, deterministic identifier for a task call."""

    parent_id: ExecutionId | None
    domain_id: DomainId
    name: ExecutionName
    sequence: int
    arguments_digest: ArgumentsDigest

    def __post_init__(self) -> None:
        # bool is an int subclass, but no persisted ordinal decoder reads its spelling.
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError(
                f"{self.sequence!r} is not an execution sequence: expected a "
                "non-negative int."
            )

    def __str__(self) -> str:
        """Return an unstable representation intended only for debugging."""
        parent_info = (
            f", parent='{self.parent_id.name}#{self.parent_id.sequence}'"
            if self.parent_id
            else ""
        )
        return (
            f"ExecutionId(domain_id='{self.domain_id}', name='{self.name}', "
            f"sequence={self.sequence}{parent_info})"
        )


@dataclass(frozen=True)
class ExecutionSequenceScope:
    """Everything of an identity except the ordinal, which counts within it."""

    parent_id: ExecutionId | None
    domain_id: DomainId
    name: ExecutionName
    arguments_digest: ArgumentsDigest

    @classmethod
    def from_execution_id(cls, execution_id: ExecutionId) -> ExecutionSequenceScope:
        return cls(
            parent_id=execution_id.parent_id,
            domain_id=execution_id.domain_id,
            name=execution_id.name,
            arguments_digest=execution_id.arguments_digest,
        )
