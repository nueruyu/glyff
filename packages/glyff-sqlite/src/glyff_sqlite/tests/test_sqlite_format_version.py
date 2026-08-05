import sqlite3
from pathlib import Path

import pytest
from glyff import Execution, SessionId, TransactionScope
from glyff.testing import canonical_arguments, make_execution_id
from glyff.exceptions import StoreFormatVersionError

from glyff_sqlite import SQLiteBackend
from glyff_sqlite._sqlite_client import FORMAT_VERSION, SQLiteClient


async def test_fresh_store_records_the_format_version(tmp_path: Path):
    db = tmp_path / "stamped.sqlite3"
    SQLiteBackend(db)

    client = SQLiteClient(db)
    rows = await client.read_sql("SELECT format_version FROM glyff_meta")

    assert rows == [(FORMAT_VERSION,)]


async def test_reopening_a_stamped_store_is_accepted(tmp_path: Path):
    db = tmp_path / "reopen.sqlite3"
    SQLiteBackend(db)
    SQLiteBackend(db)

    client = SQLiteClient(db)
    rows = await client.read_sql("SELECT format_version FROM glyff_meta")
    assert rows == [(FORMAT_VERSION,)]


def test_unknown_format_version_is_refused(tmp_path: Path):
    db = tmp_path / "future.sqlite3"
    SQLiteBackend(db)

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE glyff_meta SET format_version = ?", (FORMAT_VERSION + 1,)
    )
    connection.commit()
    connection.close()

    with pytest.raises(StoreFormatVersionError):
        SQLiteBackend(db)


def test_prefix_casing_does_not_bypass_the_version_check(tmp_path: Path):
    db = tmp_path / "casing.sqlite3"
    SQLiteBackend(db, table_prefix="glyff")

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE glyff_meta SET format_version = ?", (FORMAT_VERSION + 1,)
    )
    connection.commit()
    connection.close()

    with pytest.raises(StoreFormatVersionError):
        SQLiteBackend(db, table_prefix="GLYFF")


def test_versioning_leaves_the_databases_user_version_untouched(tmp_path: Path):
    db = tmp_path / "cohabit.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA user_version = 7")
    connection.commit()
    connection.close()

    SQLiteBackend(db)

    connection = sqlite3.connect(db)
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()
    assert user_version == 7


class TestConfigurableTablePrefix:
    def test_default_prefix_derives_both_table_names(self, tmp_path: Path):
        client = SQLiteClient(tmp_path / "default.sqlite3")
        assert client._table_name == "glyff_executions"
        assert client._sessions_table_name == "glyff_sessions"
        assert client._meta_table_name == "glyff_meta"

    async def test_custom_prefix_round_trips(self, tmp_path: Path):
        db = tmp_path / "custom.sqlite3"
        backend = SQLiteBackend(db, table_prefix="app")

        execution_id = make_execution_id("task")
        execution = Execution.start(execution_id, canonical_arguments())
        async with TransactionScope(backend.transaction_provider):
            await backend.repository.save(SessionId("custom"), execution)

        client = SQLiteClient(db, table_prefix="app")
        rows = await client.read_sql(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name IN "
            "('app_executions', 'app_meta', 'app_sessions') ORDER BY name"
        )
        assert rows == [("app_executions",), ("app_meta",), ("app_sessions",)]

        reloaded = await backend.repository.get(SessionId("custom"), execution_id)
        assert reloaded is not None

    def test_invalid_prefix_is_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="valid SQL identifier"):
            SQLiteClient(tmp_path / "bad.sqlite3", table_prefix="drop table; --")

    @pytest.mark.parametrize("prefix", ["sqlite", "SQLITE", "sqlite_app", "SQLite_App"])
    def test_sqlite_reserved_prefix_is_rejected(self, prefix: str, tmp_path: Path):
        with pytest.raises(ValueError, match="sqlite"):
            SQLiteClient(tmp_path / "reserved.sqlite3", table_prefix=prefix)
