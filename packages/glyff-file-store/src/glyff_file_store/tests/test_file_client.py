from pathlib import Path

import pytest

from glyff_file_store import FileClient
from glyff_file_store.file_client import _BACKUP_SUFFIX, _TEMP_PREFIX


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


async def test_partial_commit_failure_leaves_disk_unchanged(client: FileClient):
    """If one writer raises mid-commit, the directory-level swap is never
    performed and no staged op lands on disk. The staged ops also remain
    in place so the caller can retry."""
    # Pre-populate two files.
    (client.resolve("a.txt").parent).mkdir(exist_ok=True)
    client.resolve("a.txt").write_bytes(b"a-original")
    client.resolve("b.txt").write_bytes(b"b-original")

    async def good_writer() -> bytes:
        return b"a-new"

    async def bad_writer() -> bytes:
        raise RuntimeError("simulated writer failure")

    await client.stage_write("a.txt", good_writer)
    await client.stage_write("b.txt", bad_writer)

    with pytest.raises(RuntimeError, match="simulated writer failure"):
        await client.commit_staged()

    # Neither file changed on disk.
    assert await client.read("a.txt") == b"a-original"
    assert await client.read("b.txt") == b"b-original"


async def test_partial_commit_failure_can_be_retried(client: FileClient):
    """After a failed commit, fixing the failing writer and retrying lands
    every staged op together (because the staged state was preserved)."""
    (client.resolve("a.txt").parent).mkdir(exist_ok=True)
    client.resolve("a.txt").write_bytes(b"a-original")

    fail = True

    async def b_writer() -> bytes:
        if fail:
            raise RuntimeError("once")
        return b"b-new"

    await client.stage_write("a.txt", b"a-new")
    await client.stage_write("b.txt", b_writer)

    with pytest.raises(RuntimeError, match="once"):
        await client.commit_staged()

    # Now let b_writer succeed; the same staged ops are still there.
    fail = False
    await client.commit_staged()
    assert await client.read("a.txt") == b"a-new"
    assert await client.read("b.txt") == b"b-new"


async def test_commit_leaves_no_orphan_temp_directories(
    client: FileClient, tmp_path: Path
):
    """A successful commit cleans up its temp directory and any backup."""
    await client.stage_write("file.txt", b"content")
    await client.commit_staged()

    siblings = list(tmp_path.iterdir())
    # Only the session directory should remain — no .commit-* or .bak.
    session_name = client.resolve(".").resolve().name
    assert [s.name for s in siblings] == [session_name]


async def test_failed_commit_leaves_no_orphan_temp_directories(
    client: FileClient, tmp_path: Path
):
    """Even if a writer raises, the temp directory is cleaned up."""

    async def bad_writer() -> bytes:
        raise RuntimeError("nope")

    await client.stage_write("file.txt", bad_writer)
    with pytest.raises(RuntimeError):
        await client.commit_staged()

    session_name = client.resolve(".").resolve().name
    siblings = [s.name for s in tmp_path.iterdir()]
    assert all(
        name == session_name or not name.startswith(session_name + _TEMP_PREFIX)
        for name in siblings
    )
    # No backup either.
    assert not (tmp_path / (session_name + _BACKUP_SUFFIX)).exists()


async def test_recovery_restores_session_from_orphan_backup(tmp_path: Path):
    """A .bak sibling with no live session directory (simulating a crash
    between rename-to-backup and rename-from-temp) is restored on init."""
    session_id = "recoverable"
    (tmp_path / (session_id + _BACKUP_SUFFIX)).mkdir()
    (tmp_path / (session_id + _BACKUP_SUFFIX) / "saved.txt").write_bytes(b"saved")

    client = FileClient(base_dir=tmp_path, session_id=session_id)
    assert await client.read("saved.txt") == b"saved"
    assert not (tmp_path / (session_id + _BACKUP_SUFFIX)).exists()


async def test_recovery_drops_orphan_backup_when_session_present(tmp_path: Path):
    """A .bak sibling alongside a live session (simulating a crash after
    rename-from-temp but before rmtree-backup) is dropped on init."""
    session_id = "with-stale-bak"
    (tmp_path / session_id).mkdir()
    (tmp_path / session_id / "live.txt").write_bytes(b"live")
    (tmp_path / (session_id + _BACKUP_SUFFIX)).mkdir()
    (tmp_path / (session_id + _BACKUP_SUFFIX) / "stale.txt").write_bytes(b"stale")

    client = FileClient(base_dir=tmp_path, session_id=session_id)
    assert await client.read("live.txt") == b"live"
    assert not (tmp_path / (session_id + _BACKUP_SUFFIX)).exists()


async def test_recovery_cleans_orphan_temp_directories(tmp_path: Path):
    """Orphan .commit-* directories from prior crashed commits are deleted
    on init."""
    session_id = "with-orphan-temps"
    (tmp_path / session_id).mkdir()
    (tmp_path / (session_id + _TEMP_PREFIX + "abc123")).mkdir()
    (tmp_path / (session_id + _TEMP_PREFIX + "abc123") / "junk.txt").write_bytes(
        b""
    )
    (tmp_path / (session_id + _TEMP_PREFIX + "def456")).mkdir()

    FileClient(base_dir=tmp_path, session_id=session_id)

    siblings = {s.name for s in tmp_path.iterdir()}
    assert siblings == {session_id}


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
