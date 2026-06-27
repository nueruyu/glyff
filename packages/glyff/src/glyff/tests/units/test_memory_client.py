"""Unit tests for ``MemoryClient`` read-your-writes consistency: reads within
an uncommitted transaction must observe the staged view, consistent with
``all_keys()``."""

import asyncio

import pytest

from glyff.store import MemoryClient
from glyff.store._memory import _MemoryTransaction


async def _commit_write(client: MemoryClient, key: str, value: bytes) -> None:
    token = client.begin_staging()
    try:
        client.stage_write(key, value)
        await client.commit_staged()
    finally:
        client.end_staging(token)


async def test_read_observes_staged_write():
    client = MemoryClient()
    token = client.begin_staging()
    try:
        client.stage_write("k", b"v")
        assert await client.read("k") == b"v"
    finally:
        client.end_staging(token)


async def test_staged_write_overrides_committed_value():
    client = MemoryClient()
    await _commit_write(client, "k", b"old")

    token = client.begin_staging()
    try:
        client.stage_write("k", b"new")
        assert await client.read("k") == b"new"
        # Until commit, the committed value is untouched.
        assert client.data["k"] == b"old"
    finally:
        client.end_staging(token)


async def test_read_observes_staged_delete():
    client = MemoryClient()
    await _commit_write(client, "k", b"v")

    token = client.begin_staging()
    try:
        client.stage_delete("k")
        assert await client.read("k") is None
    finally:
        client.end_staging(token)


async def test_read_falls_through_to_committed_value():
    client = MemoryClient()
    await _commit_write(client, "k", b"v")
    assert await client.read("k") == b"v"


async def test_clear_staged_discards_staged_view():
    client = MemoryClient()
    token = client.begin_staging()
    try:
        client.stage_write("k", b"v")
        client.clear_staged()
        assert await client.read("k") is None
    finally:
        client.end_staging(token)


async def test_read_consistent_with_all_keys():
    client = MemoryClient()
    await _commit_write(client, "committed", b"c")

    token = client.begin_staging()
    try:
        client.stage_write("staged", b"s")
        client.stage_delete("committed")

        assert client.all_keys() == {"staged"}
        assert await client.read("staged") == b"s"
        assert await client.read("committed") is None
    finally:
        client.end_staging(token)


async def test_read_staged_false_returns_committed_ignoring_staged():
    client = MemoryClient()
    await _commit_write(client, "k", b"committed")

    token = client.begin_staging()
    try:
        client.stage_write("k", b"staged")
        assert await client.read("k") == b"staged"
        assert await client.read("k", staged=False) == b"committed"

        client.stage_delete("k")
        assert await client.read("k") is None
        assert await client.read("k", staged=False) == b"committed"
    finally:
        client.end_staging(token)


async def test_write_operations_require_staging():
    client = MemoryClient()

    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_write("k", b"v")
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_delete("k")
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.clear_staged()
    with pytest.raises(RuntimeError, match="outside a transaction"):
        await client.commit_staged()


async def test_all_keys_without_staging_returns_committed_keys():
    client = MemoryClient()
    await _commit_write(client, "k", b"v")

    assert client.all_keys() == {"k"}


async def test_memory_transaction_concurrent_close_finishes_once(
    monkeypatch: pytest.MonkeyPatch,
):
    client = MemoryClient()
    calls: list[str] = []
    end_calls = 0
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    original_end_staging = client.end_staging

    def end_staging(token) -> None:
        nonlocal end_calls
        end_calls += 1
        original_end_staging(token)

    async def commit_staged() -> None:
        calls.append("commit")
        commit_started.set()
        await release_commit.wait()

    def clear_staged() -> None:
        calls.append("rollback")

    monkeypatch.setattr(client, "end_staging", end_staging)
    monkeypatch.setattr(client, "commit_staged", commit_staged)
    monkeypatch.setattr(client, "clear_staged", clear_staged)

    transaction = _MemoryTransaction(client)

    commit_task = asyncio.create_task(transaction.commit())
    await commit_started.wait()

    rollback_task = asyncio.create_task(transaction.rollback())
    await asyncio.sleep(0)
    release_commit.set()

    await asyncio.gather(commit_task, rollback_task)

    assert calls == ["commit"]
    assert end_calls == 1
