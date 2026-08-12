from __future__ import annotations

from typing import Any

from glyff import DomainId, DomainVersionMap, SessionId
from glyff.exceptions import MigrationError
from glyff.migration import (
    MigrationReport,
    SessionMetadata,
    SessionMigration,
    SessionMigrator,
    StoredSession,
)
from glyff.store.aggregate_codec import execution_from_dict, execution_to_dict
from glyff.store.utils import execution_id_to_path, path_to_execution_id

from ._file_client import (
    _DOMAIN_VERSIONS_KEY,
    _EXECUTIONS_KEY,
    _SESSIONS_KEY,
    DocumentUpdate,
    FileClient,
)


class FileSessionMigration(SessionMigration):
    """Stores a migrated session through an atomic document update."""

    def __init__(self, client: FileClient):
        self._client = client

    async def run(
        self, session_id: SessionId, migrator: SessionMigrator
    ) -> MigrationReport:
        def migrate(document: dict[str, Any]) -> DocumentUpdate[MigrationReport]:
            source = self._read(document, session_id.value)
            replacement = migrator.migrate(source)

            document.setdefault(_SESSIONS_KEY, {})[session_id.value] = {
                _DOMAIN_VERSIONS_KEY: {
                    domain_id.value: version.value
                    for domain_id, version in replacement.metadata.domain_versions.items()
                },
                _EXECUTIONS_KEY: {
                    execution_id_to_path(execution.id): execution_to_dict(execution)
                    for execution in replacement.executions
                },
            }
            return DocumentUpdate(MigrationReport.between(source, replacement))

        return await self._client.update_document(migrate)

    def _read(self, document: dict[str, Any], session_id: str) -> StoredSession:
        session = document.get(_SESSIONS_KEY, {}).get(session_id, {})
        versions = session.get(_DOMAIN_VERSIONS_KEY) or {}
        if not versions:
            raise MigrationError(
                f"Session {session_id!r} has claimed no domain, so there is no "
                "version to migrate it from."
            )

        executions = session.get(_EXECUTIONS_KEY, {})
        return StoredSession(
            metadata=SessionMetadata(
                domain_versions=DomainVersionMap(
                    {
                        DomainId(domain_id): version
                        for domain_id, version in versions.items()
                    }
                )
            ),
            # Lexicographic path order is ancestor-first.
            executions=tuple(
                execution_from_dict(path_to_execution_id(path), executions[path])
                for path in sorted(executions)
            ),
        )
