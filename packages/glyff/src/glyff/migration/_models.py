from __future__ import annotations

from dataclasses import dataclass

from .._models import Execution
from ..exceptions import MigrationCollisionError


@dataclass(frozen=True)
class SessionMetadata:
    """What a store records about a session itself, rather than about its
    executions."""

    app_version: str


@dataclass(frozen=True)
class StoredSession:
    """One session as a whole: its metadata and every execution under it.

    Executions come in ancestor-first order and are unique by id, which is what
    lets a migrator remap parents before the descendants that name them. The
    uniqueness is checked here rather than at write time, so a migrator that
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
    """What one migration did.

    The versions it went between are all the mechanism knows; a migrator that
    understands the transformations records what it changed.
    """

    from_version: str
    to_version: str


@dataclass(frozen=True)
class SessionMigrationResult:
    """A migrator's answer: the session to store in place of the one it was
    given, and the report to hand back to the caller."""

    session: StoredSession
    report: MigrationReport
