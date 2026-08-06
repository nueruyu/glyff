from __future__ import annotations

from abc import ABC, abstractmethod

from .._interfaces import Backend
from .._models import SessionId
from ._models import MigrationReport, SessionMigrationResult, StoredSession


class SessionMigrator(ABC):
    """Turns one stored session into the session that should replace it.

    Pure and synchronous by contract: a backend calls this while holding the
    session exclusively, so anything that waits here holds a lock — and anything
    with a side effect happens inside a transaction that may yet be undone.
    Transformations, remapping, sequence compaction and collision detection all
    live behind this, and none of it knows how a session is stored.
    """

    @abstractmethod
    def migrate(self, source: StoredSession) -> SessionMigrationResult:
        """Returns the migrated session, performing no I/O."""
        ...


class SessionMigration(ABC):
    """A backend's ability to replace one session's recorded state wholesale.

    The mechanism half of migration, and only that: it takes the session
    exclusively, reads it, hands it to a migrator, and stores what comes back —
    metadata and executions in one atomic step, or neither of them.
    """

    @abstractmethod
    async def run(
        self, session_id: SessionId, migrator: SessionMigrator
    ) -> MigrationReport:
        """Exclusively loads, migrates, and atomically replaces one session.

        Anything raised by the migrator propagates with the session unchanged.
        """
        ...


class MigratableBackend(Backend):
    """A backend that offers migration on top of the `Backend` contract.

    Separate because migration is not part of running a session: an ephemeral
    store has no old records to carry forward and is not expected to provide it.
    """

    @property
    @abstractmethod
    def session_migration(self) -> SessionMigration: ...
