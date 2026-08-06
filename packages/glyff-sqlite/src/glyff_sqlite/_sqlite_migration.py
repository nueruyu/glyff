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
            result = migrator.migrate(source)

            self._client.delete_session_executions(connection, session_id.value)
            for execution in result.session.executions:
                self._client.upsert_execution(connection, session_id.value, execution)
            self._client.write_app_version(
                connection, session_id.value, result.session.metadata.app_version
            )
            return result.report

        return await self._client.run_immediate(migrate)

    def _read(self, connection: sqlite3.Connection, session_id: str) -> StoredSession:
        app_version = self._client.read_app_version(connection, session_id)
        if app_version is None:
            raise MigrationError(
                f"Session {session_id!r} carries no application version, so there "
                "is no version to migrate it from."
            )

        return StoredSession(
            metadata=SessionMetadata(app_version=app_version),
            # Lexicographic path order is ancestor-first.
            executions=tuple(
                record.to_execution(path_to_execution_id(path))
                for path, record in self._client.read_session_executions(
                    connection, session_id
                )
            ),
        )
