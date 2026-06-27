from pathlib import Path

from glyff.serialization import JsonSerializer
from glyff_file_store import FileClient, JsonFileSessionStore


async def test_file_transaction_close_is_idempotent(tmp_path: Path):
    store = JsonFileSessionStore(
        FileClient(base_dir=tmp_path, session_id="file-transaction"),
        JsonSerializer(),
    )
    transaction = await store.begin_transaction()
    await transaction.commit()
    await transaction.commit()
    await transaction.rollback()
    assert True
