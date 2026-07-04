from pathlib import Path

import pytest

from glyff_sqlite import SQLiteTransactionProvider
from glyff_sqlite._sqlite_client import SQLiteClient, SQLiteExecutionRecord


def record(value: str) -> SQLiteExecutionRecord:
    return SQLiteExecutionRecord(
        status="completed",
        result=f'"{value}"',
        metadata="{}",
    )


def _client(database_path: Path) -> SQLiteClient:
    client = SQLiteClient(database_path)
    client._initialize_schema_sync()
    return client


async def test_sqlite_transaction_commit_closes_and_is_idempotent(tmp_path: Path):
    client = _client(tmp_path / "commit.sqlite3")
    transaction_provider = SQLiteTransactionProvider(client)

    transaction = await transaction_provider.begin_transaction()
    client.stage_write("key", record("value"))
    await transaction.commit()
    await transaction.commit()
    await transaction.rollback()

    assert await client.read("key") == record("value")


async def test_sqlite_transaction_rollback_closes_and_is_idempotent(tmp_path: Path):
    client = _client(tmp_path / "rollback.sqlite3")
    transaction_provider = SQLiteTransactionProvider(client)

    transaction = await transaction_provider.begin_transaction()
    client.stage_write("key", record("value"))
    await transaction.rollback()
    await transaction.rollback()
    await transaction.commit()

    assert await client.read("key") is None


async def test_sqlite_transaction_out_of_order_close_raises(tmp_path: Path):
    client = _client(tmp_path / "out-of-order.sqlite3")
    transaction_provider = SQLiteTransactionProvider(client)

    parent = await transaction_provider.begin_transaction()
    child = await transaction_provider.begin_transaction()

    with pytest.raises(RuntimeError):
        await parent.commit()

    await child.rollback()
    await parent.rollback()
    assert await client.read("key") is None
