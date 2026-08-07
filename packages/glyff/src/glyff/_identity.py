"""The names a recorded execution is found by.

A leaf module: these types are what sequencing, path encoding and migration all
speak, and they depend on nothing else in glyff so that anything may depend on
them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
