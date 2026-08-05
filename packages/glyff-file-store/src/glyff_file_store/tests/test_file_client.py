"""Transaction-local staging over one JSON document."""

import asyncio
import json
import os
from pathlib import Path

import pytest
from glyff.exceptions import StoreFormatVersionError

from glyff_file_store._file_client import _STORE_FILE, _TEMP_PREFIX, FileClient

SESSION = "test-session"
FORMAT_VERSION = 1


@pytest.fixture
def client(tmp_path: Path) -> FileClient:
    return FileClient(tmp_path, format_version=FORMAT_VERSION)


def put(path: str, value: str):
    return lambda executions: {**executions, path: {"value": value}}


def drop(path: str):
    return lambda executions: {k: v for k, v in executions.items() if k != path}


def document(base_dir: Path) -> dict:
    return json.loads((base_dir / _STORE_FILE).read_text())


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
    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("root#0:abc", "kept"))
    await client.commit_staged()
    client.end_staging(t)

    text = (tmp_path / _STORE_FILE).read_text()
    assert "\n" in text and '"root#0:abc"' in text


# -- Staging -----------------------------------------------------------------


async def test_commit_applies_staged_updates(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "1"))
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read_executions(SESSION) == {"a": {"value": "1"}}


async def test_updates_apply_in_order(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "first"))
    client.stage_executions(SESSION, put("a", "second"))
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read_executions(SESSION) == {"a": {"value": "second"}}


async def test_a_read_observes_staged_updates(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "staged"))

    assert await client.read_executions(SESSION) == {"a": {"value": "staged"}}
    assert await client.read_executions(SESSION, staged=False) == {}
    client.end_staging(t)


async def test_a_staged_update_sees_the_committed_document(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "committed"))
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_executions(SESSION, put("b", "later"))
    assert set(await client.read_executions(SESSION)) == {"a", "b"}
    client.end_staging(t2)


async def test_rollback_discards_staged_updates(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "1"))
    await client.clear_staged()
    client.end_staging(t)

    assert await client.read_executions(SESSION) == {}


async def test_an_update_can_delete(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "1"))
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_executions(SESSION, drop("a"))
    await client.commit_staged()
    client.end_staging(t2)

    assert await client.read_executions(SESSION) == {}


async def test_staging_outside_a_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError):
        client.stage_executions(SESSION, put("a", "1"))


async def test_committing_outside_a_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError):
        await client.commit_staged()


# -- Across sessions ---------------------------------------------------------


async def test_one_transaction_commits_every_session_it_touched(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_executions("orders", put("a", "1"))
    client.stage_executions("refunds", put("b", "2"))
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read_executions("orders") == {"a": {"value": "1"}}
    assert await client.read_executions("refunds") == {"b": {"value": "2"}}


async def test_a_session_only_sees_its_own_records(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_executions("orders", put("shared", "orders"))
    client.stage_executions("refunds", put("shared", "refunds"))
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read_executions("orders") == {"shared": {"value": "orders"}}


# -- Durability --------------------------------------------------------------


async def test_a_failed_commit_leaves_the_document_unchanged(
    client: FileClient, tmp_path: Path
):
    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "1"))
    await client.commit_staged()
    client.end_staging(t)
    before = document(tmp_path)

    t2, _ = client.begin_staging()
    client.stage_executions(
        SESSION, lambda _: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    with pytest.raises(RuntimeError):
        await client.commit_staged()
    client.end_staging(t2)

    assert document(tmp_path) == before


async def test_a_failed_commit_leaves_no_temporary_behind(
    client: FileClient, tmp_path: Path
):
    t, _ = client.begin_staging()
    client.stage_executions(
        SESSION, lambda _: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    with pytest.raises(RuntimeError):
        await client.commit_staged()
    client.end_staging(t)

    assert not list(tmp_path.glob(_TEMP_PREFIX + "*"))


async def test_opening_a_store_clears_a_stranded_temporary(tmp_path: Path):
    FileClient(tmp_path, format_version=FORMAT_VERSION)
    (tmp_path / (_TEMP_PREFIX + "crashed")).write_text("half a document")

    FileClient(tmp_path, format_version=FORMAT_VERSION)

    assert not list(tmp_path.glob(_TEMP_PREFIX + "*"))


async def test_the_document_is_replaced_rather_than_rewritten_in_place(
    client: FileClient, tmp_path: Path
):
    # Holding the old file open across the commit is what makes lock-free reads
    # safe: the reader keeps the whole old document.
    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "before"))
    await client.commit_staged()
    client.end_staging(t)

    with open(tmp_path / _STORE_FILE, "rb") as open_before:
        t2, _ = client.begin_staging()
        client.stage_executions(SESSION, put("a", "after"))
        await client.commit_staged()
        client.end_staging(t2)

        held = json.loads(open_before.read())

    assert held["sessions"][SESSION]["executions"]["a"] == {"value": "before"}
    assert document(tmp_path)["sessions"][SESSION]["executions"]["a"] == {
        "value": "after"
    }


async def test_a_committed_document_survives_reopen(client: FileClient, tmp_path: Path):
    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "1"))
    await client.commit_staged()
    client.end_staging(t)

    reopened = FileClient(tmp_path, format_version=FORMAT_VERSION)
    assert await reopened.read_executions(SESSION) == {"a": {"value": "1"}}


# -- Concurrency -------------------------------------------------------------


async def test_concurrent_commits_do_not_lose_each_others_updates(tmp_path: Path):
    # Independent handles, because each commit read-modify-writes the whole
    # document and the later replacement would otherwise drop the earlier one.
    async def commit(client: FileClient, path: str) -> None:
        t, _ = client.begin_staging()
        client.stage_executions(SESSION, put(path, path))
        await client.commit_staged()
        client.end_staging(t)

    clients = [FileClient(tmp_path, format_version=FORMAT_VERSION) for _ in range(8)]
    await asyncio.gather(
        *(commit(client, f"path-{n}") for n, client in enumerate(clients))
    )

    recorded = await clients[0].read_executions(SESSION)
    assert set(recorded) == {f"path-{n}" for n in range(8)}


async def test_a_claim_takes_an_unclaimed_session(client: FileClient):
    assert await client.claim_session(SESSION, "v1") == "v1"
    assert await client.claim_session(SESSION, "v2") == "v1"


async def test_a_claim_does_not_disturb_recorded_executions(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "1"))
    await client.commit_staged()
    client.end_staging(t)

    await client.claim_session(SESSION, "v1")

    assert await client.read_executions(SESSION) == {"a": {"value": "1"}}


async def test_a_commit_does_not_disturb_a_claimed_version(
    client: FileClient, tmp_path: Path
):
    await client.claim_session(SESSION, "v1")

    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "1"))
    await client.commit_staged()
    client.end_staging(t)

    assert document(tmp_path)["sessions"][SESSION]["app_version"] == "v1"


async def test_a_nested_transaction_stages_independently(client: FileClient):
    outer, _ = client.begin_staging()
    client.stage_executions(SESSION, put("outer", "1"))

    inner, _ = client.begin_staging()
    client.stage_executions(SESSION, put("inner", "1"))
    assert set(await client.read_executions(SESSION)) == {"inner"}
    await client.commit_staged()
    client.end_staging(inner)

    assert set(await client.read_executions(SESSION)) == {"inner", "outer"}
    await client.clear_staged()
    client.end_staging(outer)

    assert set(await client.read_executions(SESSION)) == {"inner"}


async def test_replacement_retries_a_transient_permission_error(
    client: FileClient, monkeypatch: pytest.MonkeyPatch
):
    original_replace = os.replace
    failures = 0

    def flaky_replace(source, target):
        nonlocal failures
        if failures == 0:
            failures += 1
            raise PermissionError("simulated transient lock")
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", flaky_replace)

    t, _ = client.begin_staging()
    client.stage_executions(SESSION, put("a", "1"))
    await client.commit_staged()
    client.end_staging(t)

    assert failures == 1
    assert await client.read_executions(SESSION) == {"a": {"value": "1"}}
