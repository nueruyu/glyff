"""What a half-finished migration write leaves behind: nothing.

The shared contract covers a migrator that refuses. These are the failures only
this backend can stage — a write that dies between the executions and the
session's version row.
"""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from glyff import (
    DomainId,
    DomainVersion,
    DomainVersionMap,
    Execution,
    SessionId,
    TransactionScope,
)
from glyff.migration import (
    SessionMetadata,
    SessionMigrator,
    StoredSession,
)
from glyff.testing import canonical_arguments, make_execution_id
from glyff_sqlite import SQLiteBackend

SESSION = SessionId("migrate")
DOMAIN = DomainId("test")


class ReplacingMigrator(SessionMigrator):
    def __init__(self, *executions: Execution, version: str = "v2") -> None:
        self._executions = executions
        self._version = version

    def migrate(self, source: StoredSession) -> StoredSession:
        return StoredSession(
            metadata=SessionMetadata(DomainVersionMap({DOMAIN: self._version})),
            executions=self._executions,
        )


class RefusingConnection:
    """A real connection that refuses any statement mentioning a given word."""

    def __init__(self, connection: sqlite3.Connection, refuses: str) -> None:
        self._connection = connection
        self._refuses = refuses
        self.seen = 0

    def execute(self, sql: str, *params: Any) -> sqlite3.Cursor:
        if self._refuses in sql:
            self.seen += 1
            if self.seen > 1:  # Let the read through; refuse the write.
                raise sqlite3.OperationalError(f"refusing {self._refuses}")
        return self._connection.execute(sql, *params)

    def close(self) -> None:
        self._connection.close()


def started(name: str) -> Execution:
    return Execution.start(
        make_execution_id(name, domain_id=DOMAIN), canonical_arguments()
    )


@pytest.fixture
def backend(tmp_path: Path) -> SQLiteBackend:
    return SQLiteBackend(tmp_path / "migration.sqlite3")


async def seed(backend: SQLiteBackend, *names: str) -> list[Execution]:
    await backend.claim_domain(SESSION, DOMAIN, DomainVersion("v1"))
    seeded = []
    for name in names:
        execution = started(name)
        async with TransactionScope(backend.transaction_provider):
            await backend.repository.save(SESSION, execution)
        seeded.append(execution)
    return seeded


async def stored(backend: SQLiteBackend) -> list[Execution]:
    return [e async for e in backend.repository.executions(SESSION)]


def refuse(backend: SQLiteBackend, monkeypatch: pytest.MonkeyPatch, word: str) -> None:
    client = backend._client
    connect = client._connect
    monkeypatch.setattr(client, "_connect", lambda: RefusingConnection(connect(), word))


async def test_a_failed_execution_write_leaves_the_version_alone(
    backend: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
):
    seeded = await seed(backend, "before")
    refuse(backend, monkeypatch, "glyff_executions")

    with pytest.raises(sqlite3.OperationalError):
        await backend.session_migration.run(
            SESSION, ReplacingMigrator(started("after"))
        )

    monkeypatch.undo()
    assert [e.id for e in await stored(backend)] == [e.id for e in seeded]
    assert (
        await backend.claim_domain(SESSION, DOMAIN, DomainVersion("v-later"))
    ).value == "v1"


async def test_a_failed_version_write_leaves_the_executions_alone(
    backend: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
):
    seeded = await seed(backend, "before")
    refuse(backend, monkeypatch, "glyff_session_domains")

    with pytest.raises(sqlite3.OperationalError):
        await backend.session_migration.run(
            SESSION, ReplacingMigrator(started("after"))
        )

    monkeypatch.undo()
    assert [e.id for e in await stored(backend)] == [e.id for e in seeded]
    assert (
        await backend.claim_domain(SESSION, DOMAIN, DomainVersion("v-later"))
    ).value == "v1"


async def test_a_failed_migration_leaves_nothing_behind_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database = tmp_path / "migration.sqlite3"
    backend = SQLiteBackend(database)
    seeded = await seed(backend, "before")
    refuse(backend, monkeypatch, "glyff_session_domains")

    with pytest.raises(sqlite3.OperationalError):
        await backend.session_migration.run(
            SESSION, ReplacingMigrator(started("after"))
        )

    monkeypatch.undo()
    reopened = SQLiteBackend(database)
    assert [e.id async for e in reopened.repository.executions(SESSION)] == [
        e.id for e in seeded
    ]
    assert (
        await reopened.claim_domain(SESSION, DOMAIN, DomainVersion("v-later"))
    ).value == "v1"
