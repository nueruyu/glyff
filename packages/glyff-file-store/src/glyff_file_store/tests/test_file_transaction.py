import asyncio
from pathlib import Path

import pytest

from glyff.serialization import JsonSerializer
from glyff_file_store import FileClient, JsonFileSessionStore
from glyff_file_store._store import _FileTransaction


async def test_file_transaction_concurrent_close_finishes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = JsonFileSessionStore(
        FileClient(base_dir=tmp_path, session_id="file-transaction"),
        JsonSerializer(),
    )
    calls: list[str] = []
    end_calls = 0
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    original_store_end = store.end_staging
    original_client_end = store._client.end_staging

    def end_staging(token) -> None:
        nonlocal end_calls
        end_calls += 1
        original_store_end(token)

    def client_end_staging(token) -> None:
        nonlocal end_calls
        end_calls += 1
        original_client_end(token)

    async def commit_current() -> None:
        calls.append("commit")
        commit_started.set()
        await release_commit.wait()

    async def rollback_current() -> None:
        calls.append("rollback")

    monkeypatch.setattr(store, "end_staging", end_staging)
    monkeypatch.setattr(store._client, "end_staging", client_end_staging)
    monkeypatch.setattr(store, "_commit_current", commit_current)
    monkeypatch.setattr(store, "_rollback_current", rollback_current)

    transaction = _FileTransaction(store)

    commit_task = asyncio.create_task(transaction.commit())
    await commit_started.wait()

    rollback_task = asyncio.create_task(transaction.rollback())
    await asyncio.sleep(0)
    release_commit.set()

    await asyncio.gather(commit_task, rollback_task)

    assert calls == ["commit"]
    assert end_calls == 2
