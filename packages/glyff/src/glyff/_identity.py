"""The names a recorded execution is found by.

A leaf module: these types are what sequencing, path encoding and migration all
speak, and they depend on nothing else in glyff so that anything may depend on
them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

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
class DomainVersion:
    """A non-empty generation identifier declared by a domain owner."""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("A domain version cannot be empty.")

    def __str__(self) -> str:
        return self.value


class DomainVersionMap(Mapping[DomainId, DomainVersion]):
    """An immutable snapshot of the versions a session carries."""

    __slots__ = ("_versions",)

    def __init__(self, versions: Mapping[Any, Any]) -> None:
        if not all(
            isinstance(domain_id, (DomainId, str))
            and isinstance(version, (DomainVersion, str))
            for domain_id, version in versions.items()
        ):
            raise TypeError(
                "DomainVersionMap requires domain identifiers and domain versions."
            )
        self._versions = {
            domain_id if isinstance(domain_id, DomainId) else DomainId(domain_id): (
                version
                if isinstance(version, DomainVersion)
                else DomainVersion(version)
            )
            for domain_id, version in versions.items()
        }

    def __getitem__(self, domain_id: DomainId) -> DomainVersion:
        return self._versions[domain_id]

    def __iter__(self) -> Iterator[DomainId]:
        return iter(self._versions)

    def __len__(self) -> int:
        return len(self._versions)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DomainVersionMap) and self._versions == other._versions

    def replacing(
        self, replacements: Mapping[DomainId, DomainVersion]
    ) -> DomainVersionMap:
        return DomainVersionMap({**self._versions, **replacements})


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
    domain_id: DomainId
    name: ExecutionName
    sequence: int
    arguments_digest: ArgumentsDigest

    def __post_init__(self) -> None:
        # An ordinal a store could not read back is not an identity. ``bool`` is
        # an ``int`` subclass whose spelling no decoder accepts, so the type is
        # checked exactly rather than with ``isinstance``.
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError(
                f"{self.sequence!r} is not an execution sequence: expected a "
                "non-negative int."
            )

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
            f"ExecutionId(domain_id='{self.domain_id}', name='{self.name}', "
            f"sequence={self.sequence}{parent_info})"
        )


@dataclass(frozen=True)
class ExecutionSequenceScope:
    """Everything of an identity except the ordinal, which counts within it.

    A `sequence` orders calls inside one of these and means nothing outside it.
    Defined here so everything that reasons about ordinals shares the definition
    rather than each spelling it out.
    """

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
