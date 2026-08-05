from pathlib import Path

from glyff_file_store import FileTransactionProvider
from glyff.store._execution_stage import ExecutionStage

from glyff_file_store._file_client import FileClient


async def test_file_transaction_close_is_idempotent(tmp_path: Path):
    transaction_provider = FileTransactionProvider(
        FileClient(tmp_path, format_version=1), ExecutionStage()
    )
    transaction = await transaction_provider.begin_transaction()
    await transaction.commit()
    await transaction.commit()
    await transaction.rollback()
    assert True
