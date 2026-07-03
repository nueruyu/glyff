from pathlib import Path

from glyff.serialization import JsonSerializer
from glyff_file_store import JsonFileSessionStore
from glyff_file_store._file_client import FileClient


async def test_file_transaction_close_is_idempotent(tmp_path: Path):
    store = JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id="file-transaction"),
        serializer=JsonSerializer(),
    )
    transaction = await store.begin_transaction()
    await transaction.commit()
    await transaction.commit()
    await transaction.rollback()
    assert True
