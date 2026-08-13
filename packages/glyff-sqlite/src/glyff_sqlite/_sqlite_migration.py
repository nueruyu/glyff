from __future__ import annotations

import sqlite3

from glyff import SessionId
from glyff.exceptions import MigrationError
from glyff.migration import (
    MigrationReport,
    SessionMetadata,
    SessionMigration,
    SessionMigrator,
    StoredSession,
)
from glyff.store.utils import path_to_execution_id

from ._sqlite_client import SQLiteClient


class SQLiteSessionMigration(SessionMigration):
    """Stores a migrated session in one immediate transaction."""

    def __init__(self, client: SQLiteClient):
        self._client = client

    async def run(
        self, session_id: SessionId, migrator: SessionMigrator
    ) -> MigrationReport:
        # SQLite has no row locks, so the exclusion is the transaction itself:
        # taken before the first read and held past the last write, it makes
        # every other writer wait rather than act on what is being replaced.
        def migrate(connection: sqlite3.Connection) -> MigrationReport:
            source = self._read(connection, session_id.value)
            replacement = migrator.migrate(source)

            self._client.delete_session_executions(connection, session_id.value)
            for execution in replacement.executions:
                self._client.upsert_execution(connection, session_id.value, execution)
            self._client.replace_domain_versions(
                connection, session_id.value, replacement.metadata.domain_versions
            )
            return MigrationReport.between(source, replacement)

        return await self._client.run_immediate(migrate)

    def _read(self, connection: sqlite3.Connection, session_id: str) -> StoredSession:
        domain_versions = self._client.read_domain_versions(connection, session_id)
        if not domain_versions:
            raise MigrationError(
                f"Session {session_id!r} has claimed no domain, so there is no "
                "version to migrate it from."
            )

        return StoredSession(
            metadata=SessionMetadata(domain_versions=domain_versions),
            # Lexicographic path order is ancestor-first.
            executions=tuple(
                record.to_execution(path_to_execution_id(path))
                for path, record in self._client.read_session_executions(
                    connection, session_id
                )
            ),
        )
