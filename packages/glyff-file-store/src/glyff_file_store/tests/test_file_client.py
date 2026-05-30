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


async def test_commit_appends(client: FileClient):
    path = "test.txt"
    await client.stage_append(path, b"hello")
    await client.stage_append(path, b" world")
    await client.commit_staged()
    assert await client.read(path) == b"hello world"


async def test_commit_interleaved_write_append(client: FileClient):
    path = "test.txt"
    await client.stage_write(path, b"a")  # Content: a
    await client.stage_append(path, b"b")  # Content: ab
    await client.stage_write(path, b"c")  # Content: c
    await client.stage_append(path, b"d")  # Content: cd
    await client.commit_staged()
    assert await client.read(path) == b"cd"


async def test_delete_cancels_staged_ops(client: FileClient):
    path = "test.txt"
    # Pre-populate the file
    (client.resolve(path).parent).mkdir(exist_ok=True)
    with open(client.resolve(path), "wb") as f:
        f.write(b"initial")

    await client.stage_write(path, b"a")
    await client.stage_append(path, b"b")
    await client.stage_delete(path)
    await client.commit_staged()

    assert await client.read(path) is None


async def test_rollback_clears_all_staged_ops(client: FileClient):
    path = "test.txt"
    await client.stage_write(path, b"a")
    await client.stage_append(path, b"b")
    await client.clear_staged()
    await client.commit_staged()

    assert await client.read(path) is None


async def test_clear_callbacks_are_run_on_delete(client: FileClient):
    path = "test.txt"
    cleared_ops: list[str] = []

    async def clear_cb_a():
        cleared_ops.append("a")

    async def clear_cb_b():
        cleared_ops.append("b")

    await client.stage_write(path, b"op_a", clear_cb_a)
    await client.stage_append(path, b"op_b", clear_cb_b)

    assert not cleared_ops
    await client.stage_delete(path)
    assert sorted(cleared_ops) == ["a", "b"]

    # Ensure callbacks are not run again on commit
    cleared_ops.clear()
    await client.commit_staged()
    assert not cleared_ops


async def test_clear_callbacks_are_run_on_rollback(client: FileClient):
    path = "test.txt"
    cleared_ops: list[str] = []

    async def clear_cb_a():
        cleared_ops.append("a")

    async def clear_cb_b():
        cleared_ops.append("b")

    await client.stage_write(path, b"op_a", clear_cb_a)
    await client.stage_append(path, b"op_b", clear_cb_b)

    assert not cleared_ops
    await client.clear_staged()
    assert sorted(cleared_ops) == ["a", "b"]

    # Ensure callbacks are not run again on commit
    cleared_ops.clear()
    await client.commit_staged()
    assert not cleared_ops


async def test_commit_applies_ops_across_multiple_files(client: FileClient):
    path1 = "file1.txt"
    path2 = "file2.txt"

    await client.stage_write(path1, b"a")
    await client.stage_append(path2, b"x")
    await client.stage_append(path1, b"b")
    await client.stage_append(path2, b"y")

    await client.commit_staged()

    assert await client.read(path1) == b"ab"
    assert await client.read(path2) == b"xy"


async def test_stage_accepts_async_callback_as_content(client: FileClient):
    """The Content union accepts either bytes or a WriteCallback. This test
    exercises the callback path that all session stores rely on."""
    path = "test.txt"
    call_count = 0

    async def writer() -> bytes:
        nonlocal call_count
        call_count += 1
        return b"from callback"

    await client.stage_write(path, writer)
    assert call_count == 0  # callback only fires at commit time
    await client.commit_staged()
    assert call_count == 1
    assert await client.read(path) == b"from callback"


async def test_append_across_separate_commits_preserves_prior_content(
    client: FileClient,
):
    """Each stage_append + commit_staged cycle must preserve content written
    by previous commits. Regression test for a multi-commit append bug where
    the atomic temp-file write discarded the existing on-disk content."""
    path = "log.txt"

    await client.stage_append(path, b"first\n")
    await client.commit_staged()
    assert await client.read(path) == b"first\n"

    await client.stage_append(path, b"second\n")
    await client.commit_staged()
    assert await client.read(path) == b"first\nsecond\n"

    await client.stage_append(path, b"third\n")
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
