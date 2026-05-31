from pathlib import Path

import pytest

from glyff_file_store import FileClient


@pytest.fixture
def client(tmp_path: Path) -> FileClient:
    return FileClient(base_dir=tmp_path, session_id="test-session")


async def test_commit_single_write(client: FileClient):
    path = "test.txt"
    await client.stage_write(path, b"hello")
    await client.commit_staged()
    assert await client.read(path) == b"hello"

    await client.stage_write(path, b"world")
    await client.commit_staged()
    assert await client.read(path) == b"world"


async def test_staging_same_path_last_write_wins(client: FileClient):
    """When stage_write is called twice on the same path in one transaction,
    the later op replaces the earlier one."""
    path = "test.txt"
    await client.stage_write(path, b"first")
    await client.stage_write(path, b"second")
    await client.commit_staged()
    assert await client.read(path) == b"second"


async def test_delete_cancels_staged_write(client: FileClient):
    path = "test.txt"
    # Pre-populate the file.
    (client.resolve(path).parent).mkdir(exist_ok=True)
    with open(client.resolve(path), "wb") as f:
        f.write(b"initial")

    await client.stage_write(path, b"new")
    await client.stage_delete(path)
    await client.commit_staged()

    assert await client.read(path) is None


async def test_rollback_clears_staged_write(client: FileClient):
    path = "test.txt"
    await client.stage_write(path, b"a")
    await client.clear_staged()
    await client.commit_staged()

    assert await client.read(path) is None


async def test_clear_callback_runs_on_delete(client: FileClient):
    path = "test.txt"
    cleared: list[str] = []

    async def clear_cb():
        cleared.append("cancelled")

    await client.stage_write(path, b"data", clear_cb)
    assert cleared == []

    await client.stage_delete(path)
    assert cleared == ["cancelled"]

    # Not run again on commit.
    await client.commit_staged()
    assert cleared == ["cancelled"]


async def test_clear_callback_runs_on_rollback(client: FileClient):
    path = "test.txt"
    cleared: list[str] = []

    async def clear_cb():
        cleared.append("rolled_back")

    await client.stage_write(path, b"data", clear_cb)
    assert cleared == []

    await client.clear_staged()
    assert cleared == ["rolled_back"]

    # Not run again on commit.
    await client.commit_staged()
    assert cleared == ["rolled_back"]


async def test_clear_callback_runs_after_successful_commit(client: FileClient):
    """commit_staged runs each op's clear callback after the disk write
    succeeds, so callers can use it to release resources tied to staging."""
    path = "test.txt"
    cleared: list[str] = []

    async def clear_cb():
        cleared.append("committed")

    await client.stage_write(path, b"data", clear_cb)
    await client.commit_staged()
    assert cleared == ["committed"]
    assert await client.read(path) == b"data"


async def test_commit_applies_writes_across_multiple_files(client: FileClient):
    await client.stage_write("file1.txt", b"first-content")
    await client.stage_write("file2.txt", b"second-content")
    await client.commit_staged()

    assert await client.read("file1.txt") == b"first-content"
    assert await client.read("file2.txt") == b"second-content"


async def test_stage_accepts_async_callback_as_content(client: FileClient):
    """The Content union accepts either bytes or a WriteCallback. The
    callback fires at commit time, not stage time."""
    path = "test.txt"
    call_count = 0

    async def writer() -> bytes:
        nonlocal call_count
        call_count += 1
        return b"from callback"

    await client.stage_write(path, writer)
    assert call_count == 0
    await client.commit_staged()
    assert call_count == 1
    assert await client.read(path) == b"from callback"


async def test_callback_can_implement_append_semantics(client: FileClient):
    """Users who want 'append' semantics can implement them in a callback
    that reads existing content and concatenates the new bytes. Verified
    across multiple commits to confirm prior content is preserved."""
    path = "log.txt"

    async def make_appender(suffix: bytes):
        async def writer() -> bytes:
            existing = await client.read(path) or b""
            return existing + suffix

        return writer

    await client.stage_write(path, await make_appender(b"first\n"))
    await client.commit_staged()
    assert await client.read(path) == b"first\n"

    await client.stage_write(path, await make_appender(b"second\n"))
    await client.commit_staged()
    assert await client.read(path) == b"first\nsecond\n"

    await client.stage_write(path, await make_appender(b"third\n"))
    await client.commit_staged()
    assert await client.read(path) == b"first\nsecond\nthird\n"


async def test_stage_write_after_stage_delete_writes(client: FileClient):
    """A delete followed by a write on the same path should land as a write
    (the pending delete is cancelled by the subsequent write)."""
    path = "test.txt"
    # Pre-populate the file.
    (client.resolve(path).parent).mkdir(exist_ok=True)
    with open(client.resolve(path), "wb") as f:
        f.write(b"initial")

    await client.stage_delete(path)
    await client.stage_write(path, b"new content")
    await client.commit_staged()

    assert await client.read(path) == b"new content"
