from pathlib import Path

from glyff_file_store import FileTransactionProvider
from glyff.store.staging import ExecutionStaging

from glyff_file_store._file_client import FileClient


async def test_file_transaction_close_is_idempotent(tmp_path: Path):
    transaction_provider = FileTransactionProvider(
        FileClient(tmp_path, format_version=1), ExecutionStaging()
    )
    transaction = await transaction_provider.begin_transaction()
    await transaction.commit()
    await transaction.commit()
    await transaction.rollback()
    assert True
