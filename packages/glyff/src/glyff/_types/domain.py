"""Persistent domain identifiers and versions."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TypeVar

_DOMAIN_ID = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*")

_DomainIdInput = TypeVar("_DomainIdInput", bound="DomainId | str")
_DomainVersionInput = TypeVar("_DomainVersionInput", bound="DomainVersion | str")


def _is_domain_id(value: object) -> bool:
    return isinstance(value, (DomainId, str))


def _is_domain_version(value: object) -> bool:
    return isinstance(value, (DomainVersion, str))


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

    def __init__(
        self,
        versions: Mapping[_DomainIdInput, _DomainVersionInput],
    ) -> None:
        normalized: dict[DomainId, DomainVersion] = {}
        for domain_id, version in versions.items():
            if not _is_domain_id(domain_id) or not _is_domain_version(version):
                raise TypeError(
                    "DomainVersionMap requires domain identifiers and domain versions."
                )
            normalized_id = (
                domain_id if isinstance(domain_id, DomainId) else DomainId(domain_id)
            )
            if normalized_id in normalized:
                raise ValueError(f"{normalized_id} is specified more than once.")
            normalized[normalized_id] = (
                version
                if isinstance(version, DomainVersion)
                else DomainVersion(version)
            )
        self._versions = normalized

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
