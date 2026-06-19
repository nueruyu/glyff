"""Unit tests for ``MemoryClient`` read-your-writes consistency: reads within
an uncommitted transaction must observe the staged view, consistent with
``all_keys()``."""

from glyff.store import MemoryClient


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
