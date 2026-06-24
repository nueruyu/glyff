"""Unit tests for ``MemoryClient`` read-your-writes consistency: reads within
an uncommitted transaction must observe the staged view, consistent with
``all_keys()``."""

import asyncio

from glyff.store import MemoryClient
from glyff.store._memory import _MemoryTransaction


async def test_read_observes_staged_write():
    client = MemoryClient()
    client.stage_write("k", b"v")
    assert await client.read("k") == b"v"


async def test_staged_write_overrides_committed_value():
    client = MemoryClient()
    client.stage_write("k", b"old")
    await client.commit_staged()

    client.stage_write("k", b"new")
    assert await client.read("k") == b"new"
    # Until commit, the committed value is untouched.
    assert client.data["k"] == b"old"


async def test_read_observes_staged_delete():
    client = MemoryClient()
    client.stage_write("k", b"v")
    await client.commit_staged()

    client.stage_delete("k")
    assert await client.read("k") is None


async def test_read_falls_through_to_committed_value():
    client = MemoryClient()
    client.stage_write("k", b"v")
    await client.commit_staged()
    assert await client.read("k") == b"v"


async def test_clear_staged_discards_staged_view():
    client = MemoryClient()
    client.stage_write("k", b"v")
    client.clear_staged()
    assert await client.read("k") is None


async def test_read_consistent_with_all_keys():
    client = MemoryClient()
    client.stage_write("committed", b"c")
    await client.commit_staged()

    client.stage_write("staged", b"s")
    client.stage_delete("committed")

    assert client.all_keys() == {"staged"}
    assert await client.read("staged") == b"s"
    assert await client.read("committed") is None


async def test_read_staged_false_returns_committed_ignoring_staged():
    client = MemoryClient()
    client.stage_write("k", b"committed")
    await client.commit_staged()

    client.stage_write("k", b"staged")
    assert await client.read("k") == b"staged"
    assert await client.read("k", staged=False) == b"committed"

    client.stage_delete("k")
    assert await client.read("k") is None
    assert await client.read("k", staged=False) == b"committed"


async def test_memory_transaction_concurrent_close_finishes_once():
    class FakeClient:
        def __init__(self):
            self.calls: list[str] = []
            self.end_calls = 0
            self.commit_started = asyncio.Event()
            self.release_commit = asyncio.Event()

        def begin_staging(self):
            return object()

        def end_staging(self, token) -> None:
            self.end_calls += 1

        async def commit_staged(self) -> None:
            self.calls.append("commit")
            self.commit_started.set()
            await self.release_commit.wait()

        def clear_staged(self) -> None:
            self.calls.append("rollback")

    client = FakeClient()
    transaction = _MemoryTransaction(client)  # type: ignore[arg-type]

    commit_task = asyncio.create_task(transaction.commit())
    await client.commit_started.wait()

    rollback_task = asyncio.create_task(transaction.rollback())
    await asyncio.sleep(0)
    client.release_commit.set()

    await asyncio.gather(commit_task, rollback_task)

    assert client.calls == ["commit"]
    assert client.end_calls == 1
