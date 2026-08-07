from __future__ import annotations

from abc import ABC, abstractmethod

from .._interfaces import Backend
from .._identity import SessionId
from ._models import MigrationReport, SessionMigrationResult, StoredSession


class SessionMigrator(ABC):
    """Computes the replacement state for one stored session."""

    @abstractmethod
    def migrate(self, source: StoredSession) -> SessionMigrationResult:
        """Returns a replacement without performing I/O.

        ``source.executions`` comes in ancestor-first order, so a parent can be
        remapped before whatever names it. A backend holds the session for the
        duration of this call, so waiting here holds a lock and a side effect
        here happens inside a transaction that may yet be undone.
        """
        ...


class SessionMigration(ABC):
    """Atomically migrates one session in a backend."""

    @abstractmethod
    async def run(
        self, session_id: SessionId, migrator: SessionMigrator
    ) -> MigrationReport:
        """Runs the migrator under exclusive storage access and stores its result.

        The caller must ensure the session is offline: exclusion lasts only for
        this call, and nothing stops a worker on the previous application
        version from writing to the session once it returns.

        Anything the migrator raises propagates with the session unchanged.
        """
        ...


class MigratableBackend(Backend):
    """A backend that can migrate a session between application versions."""

    @property
    @abstractmethod
    def session_migration(self) -> SessionMigration: ...
