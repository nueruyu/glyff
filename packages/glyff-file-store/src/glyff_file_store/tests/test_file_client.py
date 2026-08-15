"""Committed document I/O: stamping, atomic replacement, and locking."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from glyff import DomainId, Execution, SessionId
from glyff.exceptions import StoreFormatVersionError
from glyff.store.staging import (
    DeleteExecution,
    ExecutionKey,
    ExecutionSnapshot,
    SaveExecution,
)
from glyff.store.utils import execution_id_to_path
from glyff.testing import canonical_arguments, make_execution_id

from glyff_file_store._file_client import STORE_FILE, TEMP_PREFIX, FileClient

SESSION = SessionId("test-session")
DOMAIN = DomainId("test")
FORMAT_VERSION = 1


@pytest.fixture
def client(tmp_path: Path) -> FileClient:
    return FileClient(tmp_path, format_version=FORMAT_VERSION)


def save(
    name: str, *, session: SessionId = SESSION
) -> tuple[ExecutionKey, SaveExecution]:
    execution = Execution.start(make_execution_id(name), canonical_arguments())
    return (
        ExecutionKey(session, execution.id),
        SaveExecution(ExecutionSnapshot.from_execution(execution)),
    )


def path_of(name: str) -> str:
    return execution_id_to_path(make_execution_id(name))


def document(base_dir: Path) -> dict[str, Any]:
    return json.loads((base_dir / STORE_FILE).read_text())


# -- The document ------------------------------------------------------------


async def test_a_fresh_store_is_stamped_with_the_format_version(tmp_path: Path):
    FileClient(tmp_path, format_version=FORMAT_VERSION)

    assert document(tmp_path) == {"format_version": FORMAT_VERSION, "sessions": {}}


async def test_reopening_a_stamped_store_is_accepted(tmp_path: Path):
    FileClient(tmp_path, format_version=FORMAT_VERSION)
    FileClient(tmp_path, format_version=FORMAT_VERSION)

    assert document(tmp_path)["format_version"] == FORMAT_VERSION


def test_an_unknown_format_version_is_refused(tmp_path: Path):
    FileClient(tmp_path, format_version=FORMAT_VERSION)

    with pytest.raises(StoreFormatVersionError):
        FileClient(tmp_path, format_version=FORMAT_VERSION + 1)


async def test_the_document_stays_readable(client: FileClient, tmp_path: Path):
    key, mutation = save("task")
    await client.commit_mutations({key: mutation})

    text = (tmp_path / STORE_FILE).read_text()
    assert "\n" in text and path_of("task") in text


# -- Commits -----------------------------------------------------------------


async def test_a_committed_save_is_readable(client: FileClient):
    key, mutation = save("task")

    await client.commit_mutations({key: mutation})

    executions = await client.read_committed_executions(SESSION.value)
    assert set(executions) == {path_of("task")}


async def test_a_committed_delete_removes_the_record(client: FileClient):
    key, mutation = save("task")
    await client.commit_mutations({key: mutation})

    await client.commit_mutations({key: DeleteExecution()})

    assert await client.read_committed_executions(SESSION.value) == {}


async def test_one_commit_covers_every_session_it_touched(client: FileClient):
    orders, orders_save = save("task", session=SessionId("orders"))
    refunds, refunds_save = save("task", session=SessionId("refunds"))

    await client.commit_mutations({orders: orders_save, refunds: refunds_save})

    assert await client.read_committed_executions("orders") != {}
    assert await client.read_committed_executions("refunds") != {}


async def test_a_session_only_sees_its_own_records(client: FileClient):
    orders, orders_save = save("task", session=SessionId("orders"))
    await client.commit_mutations({orders: orders_save})

    assert await client.read_committed_executions("refunds") == {}


async def test_an_empty_batch_writes_nothing(client: FileClient, tmp_path: Path):
    before = document(tmp_path)

    await client.commit_mutations({})

    assert document(tmp_path) == before


# -- Durability --------------------------------------------------------------


async def test_opening_a_store_clears_a_stranded_temporary(tmp_path: Path):
    FileClient(tmp_path, format_version=FORMAT_VERSION)
    (tmp_path / (TEMP_PREFIX + "crashed")).write_text("half a document")

    FileClient(tmp_path, format_version=FORMAT_VERSION)

    assert not list(tmp_path.glob(TEMP_PREFIX + "*"))


async def test_the_document_is_replaced_rather_than_rewritten_in_place(
    client: FileClient, tmp_path: Path
):
    # Holding the old file open across the commit is what makes lock-free reads
    # safe: the reader keeps the whole old document.
    key, mutation = save("before")
    await client.commit_mutations({key: mutation})

    with open(tmp_path / STORE_FILE, "rb") as open_before:
        later, later_save = save("after")
        await client.commit_mutations({later: later_save})

        held = json.loads(open_before.read())

    assert set(held["sessions"][SESSION.value]["executions"]) == {path_of("before")}
    assert set(document(tmp_path)["sessions"][SESSION.value]["executions"]) == {
        path_of("before"),
        path_of("after"),
    }


async def test_a_committed_document_survives_reopen(client: FileClient, tmp_path: Path):
    key, mutation = save("task")
    await client.commit_mutations({key: mutation})

    reopened = FileClient(tmp_path, format_version=FORMAT_VERSION)
    assert set(await reopened.read_committed_executions(SESSION.value)) == {
        path_of("task")
    }


# -- Concurrency -------------------------------------------------------------


async def test_concurrent_commits_do_not_lose_each_others_records(tmp_path: Path):
    # Independent handles, because each commit read-modify-writes the whole
    # document and the later replacement would otherwise drop the earlier one.
    clients = [FileClient(tmp_path, format_version=FORMAT_VERSION) for _ in range(8)]

    await asyncio.gather(
        *(
            client.commit_mutations(dict([save(f"task-{n}")]))
            for n, client in enumerate(clients)
        )
    )

    executions = await clients[0].read_committed_executions(SESSION.value)
    assert set(executions) == {path_of(f"task-{n}") for n in range(8)}


async def test_a_claim_takes_an_unclaimed_domain(client: FileClient):
    assert await client.claim_domain(SESSION.value, DOMAIN, "v1") == "v1"
    assert await client.claim_domain(SESSION.value, DOMAIN, "v2") == "v1"


async def test_a_claim_on_a_claimed_domain_writes_nothing(
    client: FileClient, monkeypatch: pytest.MonkeyPatch
):
    # Reading the recorded version is the whole operation. Replacing the store
    # to say so would re-serialize and fsync every session in it.
    await client.claim_domain(SESSION.value, DOMAIN, "v1")

    def refuse(document: dict[str, Any]) -> None:
        raise AssertionError("a claim that changed nothing rewrote the store")

    monkeypatch.setattr(client, "_write_document_sync", refuse)

    assert await client.claim_domain(SESSION.value, DOMAIN, "v2") == "v1"


async def test_a_claim_does_not_disturb_recorded_executions(client: FileClient):
    key, mutation = save("task")
    await client.commit_mutations({key: mutation})

    await client.claim_domain(SESSION.value, DOMAIN, "v1")

    assert set(await client.read_committed_executions(SESSION.value)) == {
        path_of("task")
    }


async def test_a_commit_does_not_disturb_a_claimed_version(
    client: FileClient, tmp_path: Path
):
    await client.claim_domain(SESSION.value, DOMAIN, "v1")

    key, mutation = save("task")
    await client.commit_mutations({key: mutation})

    assert document(tmp_path)["sessions"][SESSION.value]["domain_versions"] == {
        DOMAIN.value: "v1"
    }
