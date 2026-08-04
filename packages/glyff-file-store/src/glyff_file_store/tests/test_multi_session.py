"""One base directory holds many sessions, each swapped into place on its own."""

from pathlib import Path

import pytest
from glyff import Execution, SessionId, TransactionScope
from glyff.testing import canonical_arguments, make_execution_id, serialized_value

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


@pytest.mark.parametrize(
    ("session_id", "directory"),
    [
        ("orders", "orders"),
        ("chat-2026_01", "chat-2026_01"),
        ("..", "%2E%2E"),
        (".hidden", "%2Ehidden"),
        ("a/b", "a%2Fb"),
        ("a b", "a%20b"),
        ("100%", "100%25"),
        ("注文", "%E6%B3%A8%E6%96%87"),
    ],
)
async def test_a_session_name_is_encoded_into_one_directory(
    tmp_path: Path, session_id: str, directory: str
):
    # Encoded, not validated: an application names its sessions and the store
    # makes that safe to put on a filesystem.
    backend = JsonFileBackend(base_dir=tmp_path)
    execution_id = make_execution_id("task")

    async with TransactionScope(backend.transaction_provider):
        await backend.repository.save(
            SessionId(session_id), Execution.start(execution_id, canonical_arguments())
        )

    assert (tmp_path / directory / "executions.json").exists()
    loaded = await backend.repository.get(SessionId(session_id), execution_id)
    assert loaded is not None


async def test_names_that_differ_only_by_escaping_stay_apart(tmp_path: Path):
    # '%2E' must not decode onto '.', or two sessions would share a directory.
    backend = JsonFileBackend(base_dir=tmp_path)
    execution_id = make_execution_id("task")

    for session_id, result in ((".", "dot"), ("%2E", "escaped")):
        execution = Execution.start(execution_id, canonical_arguments())
        execution.complete(serialized_value(result))
        async with TransactionScope(backend.transaction_provider):
            await backend.repository.save(SessionId(session_id), execution)

    dot = await backend.repository.get(SessionId("."), execution_id)
    escaped = await backend.repository.get(SessionId("%2E"), execution_id)
    assert dot is not None and dot.result == serialized_value("dot")
    assert escaped is not None and escaped.result == serialized_value("escaped")
