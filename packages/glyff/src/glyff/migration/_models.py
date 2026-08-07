from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .._models import DomainId, Execution, ExecutionId
from ..exceptions import MigrationCollisionError, MigrationError


def _read_only(versions: Mapping[DomainId, str]) -> Mapping[DomainId, str]:
    return MappingProxyType(dict(versions))


@dataclass(frozen=True)
class SessionMetadata:
    """What a store records about a session itself, not about its executions."""

    domain_versions: Mapping[DomainId, str]

    def __post_init__(self) -> None:
        # Copied: ``frozen`` protects the attribute, not the mapping behind it.
        object.__setattr__(self, "domain_versions", _read_only(self.domain_versions))


@dataclass(frozen=True)
class StoredSession:
    """One session's snapshot: its metadata and its executions, unique by id.

    Both invariants are checked here rather than at write time, so a migrator
    that produces an unstorable session is refused before a store can keep part
    of it.
    """

    metadata: SessionMetadata
    executions: tuple[Execution, ...]

    def __post_init__(self) -> None:
        seen = set()
        for execution in self.executions:
            if execution.id in seen:
                raise MigrationCollisionError(
                    f"Session holds more than one execution with id {execution.id}."
                )
            seen.add(execution.id)

        # Every domain named anywhere in an identity chain, not just the leaf's
        # own: a parent's domain is part of its descendants' keys, so dropping
        # its version would leave records nothing has agreed a generation for.
        recorded = set(self.metadata.domain_versions)
        missing = sorted(
            domain.value
            for domain in {
                domain
                for execution in self.executions
                for domain in _domains_in(execution.id)
            }
            - recorded
        )
        if missing:
            named = ", ".join(missing)
            raise MigrationError(
                f"Session holds executions in {named}, but records no version "
                "for them. Every domain in an execution identity chain needs "
                "one."
            )


def _domains_in(execution_id: ExecutionId) -> set[DomainId]:
    domains = set()
    current: ExecutionId | None = execution_id
    while current is not None:
        domains.add(current.domain)
        current = current.parent_id
    return domains


@dataclass(frozen=True)
class MigrationReport:
    """The versions recorded before and after a migration."""

    from_domain_versions: Mapping[DomainId, str]
    to_domain_versions: Mapping[DomainId, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "from_domain_versions", _read_only(self.from_domain_versions)
        )
        object.__setattr__(
            self, "to_domain_versions", _read_only(self.to_domain_versions)
        )


@dataclass(frozen=True)
class SessionMigrationResult:
    """The session to store in place of the migrated one, and its report."""

    session: StoredSession
    report: MigrationReport
