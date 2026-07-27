import sqlite3
from pathlib import Path

import pytest
from glyff import Execution, ExecutionId, TransactionScope
from glyff.exceptions import StoreFormatVersionError

from glyff_sqlite import SQLiteBackend
from glyff_sqlite._sqlite_client import FORMAT_VERSION, SQLiteClient


async def test_fresh_table_records_the_format_version(tmp_path: Path):
    db = tmp_path / "stamped.sqlite3"
    SQLiteBackend(db)

    client = SQLiteClient(db)
    rows = await client.read_sql(
        "SELECT format_version FROM glyff_meta WHERE table_name = ?",
        "glyff_executions",
    )

    assert rows == [(FORMAT_VERSION,)]


async def test_reopening_a_stamped_table_is_accepted(tmp_path: Path):
    db = tmp_path / "reopen.sqlite3"
    SQLiteBackend(db)
    SQLiteBackend(db)

    client = SQLiteClient(db)
    rows = await client.read_sql(
        "SELECT format_version FROM glyff_meta WHERE table_name = ?",
        "glyff_executions",
    )
    assert rows == [(FORMAT_VERSION,)]


def test_unknown_format_version_is_refused(tmp_path: Path):
    db = tmp_path / "future.sqlite3"
    SQLiteBackend(db)

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE glyff_meta SET format_version = ? WHERE table_name = ?",
        (FORMAT_VERSION + 1, "glyff_executions"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(StoreFormatVersionError):
        SQLiteBackend(db)


def test_table_name_casing_does_not_bypass_the_version_check(tmp_path: Path):
    # A differently-cased name is the same physical table, so it must see the
    # recorded version rather than key a fresh row.
    db = tmp_path / "casing.sqlite3"
    SQLiteBackend(db, table_name="glyff_executions")

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE glyff_meta SET format_version = ? WHERE table_name = ?",
        (FORMAT_VERSION + 1, "glyff_executions"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(StoreFormatVersionError):
        SQLiteBackend(db, table_name="GLYFF_EXECUTIONS")


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


class TestConfigurableTableName:
    def test_default_table_name(self, tmp_path: Path):
        client = SQLiteClient(tmp_path / "default.sqlite3")
        assert client._table_name == "glyff_executions"

    async def test_custom_table_name_round_trips(self, tmp_path: Path):
        db = tmp_path / "custom.sqlite3"
        backend = SQLiteBackend(db, table_name="app_glyff")

        execution_id = ExecutionId(
            parent_id=None, name="task", sequence=0, args_hash="hash"
        )
        execution = Execution.start(execution_id)
        async with TransactionScope(backend.transaction_provider):
            await backend.repository.save(execution)

        client = SQLiteClient(db, table_name="app_glyff")
        rows = await client.read_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'app_glyff'"
        )
        assert rows == [("app_glyff",)]

        reloaded = await backend.repository.get(execution_id)
        assert reloaded is not None

    def test_invalid_table_name_is_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError, match="valid SQL identifier"):
            SQLiteClient(tmp_path / "bad.sqlite3", table_name="drop table; --")

    @pytest.mark.parametrize("name", ["glyff_meta", "GLYFF_META", "Glyff_Meta"])
    def test_metadata_table_name_is_reserved(self, name: str, tmp_path: Path):
        with pytest.raises(ValueError, match="reserved"):
            SQLiteClient(tmp_path / "reserved.sqlite3", table_name=name)
