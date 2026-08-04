"""One base directory holds many sessions, each swapped into place on its own."""

from pathlib import Path

from glyff import Execution, SessionId, TransactionScope
from glyff.testing import canonical_arguments, make_execution_id

from glyff_file_store import JsonFileBackend

ORDERS = SessionId("orders")
REFUNDS = SessionId("refunds")


async def test_each_session_gets_its_own_directory(tmp_path: Path):
    backend = JsonFileBackend(base_dir=tmp_path)
    execution_id = make_execution_id("task")

    for session_id in (ORDERS, REFUNDS):
        async with TransactionScope(backend.transaction_provider):
            await backend.repository.save(
                session_id, Execution.start(execution_id, canonical_arguments())
            )

    assert (tmp_path / "orders" / "executions.json").exists()
    assert (tmp_path / "refunds" / "executions.json").exists()


async def test_a_claimed_version_lives_beside_its_own_session(tmp_path: Path):
    backend = JsonFileBackend(base_dir=tmp_path)

    assert await backend.claim_session(ORDERS, "v1") == "v1"
    assert await backend.claim_session(REFUNDS, "v2") == "v2"

    assert await backend.claim_session(ORDERS, None) == "v1"
    assert (tmp_path / "glyff_format.json").exists()
