"""Persistent domain identifiers and versions."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

_DOMAIN_ID = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*")


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
