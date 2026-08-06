from __future__ import annotations

from dataclasses import dataclass

from .._models import Execution
from ..exceptions import MigrationCollisionError


@dataclass(frozen=True)
class SessionMetadata:
    """What a store records about a session itself, not about its executions."""

    app_version: str


@dataclass(frozen=True)
class StoredSession:
    """One session's snapshot: its metadata and its executions, unique by id.

    Uniqueness is checked here rather than at write time, so a migrator that
    lands two executions on one id is refused before a store can silently keep
    the last of them.
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


@dataclass(frozen=True)
class MigrationReport:
    """The versions recorded before and after a migration."""

    from_version: str
    to_version: str


@dataclass(frozen=True)
class SessionMigrationResult:
    """The session to store in place of the migrated one, and its report."""

    session: StoredSession
    report: MigrationReport
