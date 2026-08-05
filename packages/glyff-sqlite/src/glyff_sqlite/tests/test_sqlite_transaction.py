from pathlib import Path

import pytest

from glyff import Execution, SessionId
from glyff.store.staging import ExecutionStaging
from glyff.testing import canonical_arguments, make_execution_id

from glyff_sqlite import SQLiteExecutionRepository, SQLiteTransactionProvider
from glyff_sqlite._sqlite_client import SQLiteClient

SESSION = SessionId("s")


def _started() -> Execution:
    return Execution.start(make_execution_id("task"), canonical_arguments())


async def _committed(client: SQLiteClient) -> object:
    return await client.read_committed(SESSION.value, "task#0:" + _digest())


def _digest() -> str:
    return make_execution_id("task").arguments_digest


def _client(database_path: Path) -> SQLiteClient:
    client = SQLiteClient(database_path)
    client._initialize_schema_sync()
    return client


async def test_sqlite_transaction_commit_closes_and_is_idempotent(tmp_path: Path):
    client = _client(tmp_path / "commit.sqlite3")
    staging = ExecutionStaging()
    repository = SQLiteExecutionRepository(client, staging)
    transaction_provider = SQLiteTransactionProvider(client, staging)

    transaction = await transaction_provider.begin_transaction()
    await repository.save(SESSION, _started())
    await transaction.commit()
    await transaction.commit()
    await transaction.rollback()

    assert await _committed(client) is not None


async def test_sqlite_transaction_rollback_closes_and_is_idempotent(tmp_path: Path):
    client = _client(tmp_path / "rollback.sqlite3")
    staging = ExecutionStaging()
    repository = SQLiteExecutionRepository(client, staging)
    transaction_provider = SQLiteTransactionProvider(client, staging)

    transaction = await transaction_provider.begin_transaction()
    await repository.save(SESSION, _started())
    await transaction.rollback()
    await transaction.rollback()
    await transaction.commit()

    assert await _committed(client) is None


async def test_sqlite_transaction_out_of_order_close_raises(tmp_path: Path):
    client = _client(tmp_path / "out-of-order.sqlite3")
    transaction_provider = SQLiteTransactionProvider(client, ExecutionStaging())

    parent = await transaction_provider.begin_transaction()
    child = await transaction_provider.begin_transaction()

    with pytest.raises(RuntimeError):
        await parent.commit()

    await child.rollback()
    await parent.rollback()
    assert await _committed(client) is None
