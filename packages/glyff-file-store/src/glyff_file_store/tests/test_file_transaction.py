from pathlib import Path

from glyff_file_store import FileTransactionProvider
from glyff_file_store._file_client import FileClient


async def test_file_transaction_close_is_idempotent(tmp_path: Path):
    transaction_provider = FileTransactionProvider(FileClient(base_dir=tmp_path))
    transaction = await transaction_provider.begin_transaction()
    await transaction.commit()
    await transaction.commit()
    await transaction.rollback()
    assert True
