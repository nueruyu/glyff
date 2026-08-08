"""Connection and transaction lifecycle: what the client guarantees around a
block of SQL, including the failure paths a real database will not produce."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from glyff_sqlite._sqlite_client import SQLiteClient

_INSERT = (
    "INSERT INTO glyff_session_domains (session_id, domain_id, version) "
    "VALUES ('s', 'd', 'v1')"
)
_SELECT = "SELECT version FROM glyff_session_domains WHERE session_id = 's'"


@pytest.fixture
def client(tmp_path: Path) -> SQLiteClient:
    client = SQLiteClient(tmp_path / "lifecycle.sqlite3")
    client._initialize_schema_sync()
    return client


class FlakyConnection:
    """A real connection whose named statements refuse to run.

    Standing in for the failures that matter here — a commit or a rollback that
    does not land — which a healthy database will not produce on demand.
    """

    def __init__(self, connection: sqlite3.Connection, *, failing: set[str]) -> None:
        self._connection = connection
        self._failing = failing
        self.statements: list[str] = []
        self.closed = False

    def execute(self, sql: str, *params: Any) -> sqlite3.Cursor:
        self.statements.append(sql)
        if sql in self._failing:
            raise sqlite3.OperationalError(f"{sql} refused")
        return self._connection.execute(sql, *params)

    def close(self) -> None:
        self.closed = True
        self._connection.close()


def flaky(
    client: SQLiteClient, monkeypatch: pytest.MonkeyPatch, *, failing: set[str]
) -> FlakyConnection:
    connection = FlakyConnection(client._connect(), failing=failing)
    monkeypatch.setattr(client, "_connect", lambda: connection)
    return connection


# -- The connection ----------------------------------------------------------


def test_a_connection_closes_after_the_block(client: SQLiteClient):
    with client._connection() as connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_a_connection_closes_when_the_block_raises(client: SQLiteClient):
    with pytest.raises(ValueError, match="boom"):
        with client._connection() as connection:
            raise ValueError("boom")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_a_read_does_not_open_a_transaction(client: SQLiteClient):
    with client._connection() as connection:
        connection.execute("SELECT 1")
        assert not connection.in_transaction


# -- The transaction ---------------------------------------------------------


async def test_a_transaction_commits_what_the_block_wrote(client: SQLiteClient):
    with client._immediate_transaction() as connection:
        connection.execute(_INSERT)

    assert await client.read_sql(_SELECT) == [("v1",)]


async def test_a_transaction_rolls_back_when_the_block_raises(client: SQLiteClient):
    with pytest.raises(ValueError, match="boom"):
        with client._immediate_transaction() as connection:
            connection.execute(_INSERT)
            raise ValueError("boom")

    assert await client.read_sql(_SELECT) == []


def test_a_transaction_closes_its_connection_either_way(
    client: SQLiteClient, monkeypatch: pytest.MonkeyPatch
):
    connection = flaky(client, monkeypatch, failing=set())

    with client._immediate_transaction():
        pass

    assert connection.closed


def test_a_failing_begin_propagates_and_never_enters_the_block(
    client: SQLiteClient, monkeypatch: pytest.MonkeyPatch
):
    connection = flaky(client, monkeypatch, failing={"BEGIN IMMEDIATE"})
    entered = False

    with pytest.raises(sqlite3.OperationalError, match="BEGIN IMMEDIATE refused"):
        with client._immediate_transaction():
            entered = True

    assert not entered
    # Nothing began, so there is nothing to roll back.
    assert "ROLLBACK" not in connection.statements
    assert connection.closed


def test_a_failing_commit_rolls_back_and_propagates_the_commit_error(
    client: SQLiteClient, monkeypatch: pytest.MonkeyPatch
):
    connection = flaky(client, monkeypatch, failing={"COMMIT"})

    with pytest.raises(sqlite3.OperationalError, match="COMMIT refused"):
        with client._immediate_transaction() as open_connection:
            open_connection.execute(_INSERT)

    assert "ROLLBACK" in connection.statements
    assert connection.closed


def test_a_failing_rollback_does_not_replace_the_error_that_caused_it(
    client: SQLiteClient, monkeypatch: pytest.MonkeyPatch
):
    connection = flaky(client, monkeypatch, failing={"ROLLBACK"})

    with pytest.raises(ValueError, match="boom"):
        with client._immediate_transaction():
            raise ValueError("boom")

    assert "ROLLBACK" in connection.statements
    assert connection.closed
