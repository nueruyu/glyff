import asyncio
import os
from pathlib import Path

import pytest
from glyff import ExecutionId, ExecutionStatus

from glyff_file_store import FileClient, JsonFileSessionStore
from glyff_file_store._file_client import _BACKUP_SUFFIX, _TEMP_PREFIX


@pytest.fixture
def client(tmp_path: Path) -> FileClient:
    return FileClient(base_dir=tmp_path, session_id="test-session")


async def test_commit_single_write(client: FileClient):
    path = "test.txt"
    t1, _ = client.begin_staging()
    await client.stage_write(path, b"hello")
    await client.commit_staged()
    client.end_staging(t1)
    assert await client.read(path) == b"hello"

    t2, _ = client.begin_staging()
    await client.stage_write(path, b"world")
    await client.commit_staged()
    client.end_staging(t2)
    assert await client.read(path) == b"world"


async def test_staging_same_path_last_write_wins(client: FileClient):
    """When stage_write is called twice on the same path in one transaction,
    the later op replaces the earlier one."""
    path = "test.txt"
    t, _ = client.begin_staging()
    await client.stage_write(path, b"first")
    await client.stage_write(path, b"second")
    await client.commit_staged()
    client.end_staging(t)
    assert await client.read(path) == b"second"


async def test_delete_cancels_staged_write(client: FileClient):
    path = "test.txt"
    (client.resolve(path).parent).mkdir(exist_ok=True)
    with open(client.resolve(path), "wb") as f:
        f.write(b"initial")

    t, _ = client.begin_staging()
    await client.stage_write(path, b"new")
    await client.stage_delete(path)
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read(path) is None


async def test_rollback_clears_staged_write(client: FileClient):
    path = "test.txt"
    t, _ = client.begin_staging()
    await client.stage_write(path, b"a")
    await client.clear_staged()
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read(path) is None


async def test_clear_callback_runs_on_delete(client: FileClient):
    path = "test.txt"
    cleared: list[str] = []

    async def clear_cb():
        cleared.append("cancelled")

    t, _ = client.begin_staging()
    await client.stage_write(path, b"data", clear_cb)
    assert cleared == []

    await client.stage_delete(path)
    assert cleared == ["cancelled"]
    client.end_staging(t)


async def test_clear_callback_runs_on_rollback(client: FileClient):
    path = "test.txt"
    cleared: list[str] = []

    async def clear_cb():
        cleared.append("rolled_back")

    t, _ = client.begin_staging()
    await client.stage_write(path, b"data", clear_cb)
    assert cleared == []

    await client.clear_staged()
    assert cleared == ["rolled_back"]
    client.end_staging(t)


async def test_clear_callback_runs_after_successful_commit(client: FileClient):
    """commit_staged runs each op's clear callback after the disk write
    succeeds, so callers can use it to release resources tied to staging."""
    path = "test.txt"
    cleared: list[str] = []

    async def clear_cb():
        cleared.append("committed")

    t, _ = client.begin_staging()
    await client.stage_write(path, b"data", clear_cb)
    await client.commit_staged()
    client.end_staging(t)
    assert cleared == ["committed"]
    assert await client.read(path) == b"data"


async def test_commit_applies_writes_across_multiple_files(client: FileClient):
    t, _ = client.begin_staging()
    await client.stage_write("file1.txt", b"first-content")
    await client.stage_write("file2.txt", b"second-content")
    await client.commit_staged()
    client.end_staging(t)

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

    t, _ = client.begin_staging()
    await client.stage_write(path, writer)
    assert call_count == 0
    await client.commit_staged()
    client.end_staging(t)
    assert call_count == 1
    assert await client.read(path) == b"from callback"


async def test_callback_can_implement_append_semantics(client: FileClient):
    """Users who want 'append' semantics can implement them in a callback
    that reads existing committed content (staged=False) and concatenates the
    new bytes. Verified across multiple commits to confirm prior content is
    preserved."""
    path = "log.txt"

    async def make_appender(suffix: bytes):
        async def writer() -> bytes:
            existing = await client.read(path, staged=False) or b""
            return existing + suffix

        return writer

    t1, _ = client.begin_staging()
    await client.stage_write(path, await make_appender(b"first\n"))
    await client.commit_staged()
    client.end_staging(t1)
    assert await client.read(path) == b"first\n"

    t2, _ = client.begin_staging()
    await client.stage_write(path, await make_appender(b"second\n"))
    await client.commit_staged()
    client.end_staging(t2)
    assert await client.read(path) == b"first\nsecond\n"

    t3, _ = client.begin_staging()
    await client.stage_write(path, await make_appender(b"third\n"))
    await client.commit_staged()
    client.end_staging(t3)
    assert await client.read(path) == b"first\nsecond\nthird\n"


async def test_partial_commit_failure_leaves_disk_unchanged(client: FileClient):
    """If one writer raises mid-commit, the directory-level swap is never
    performed and no staged op lands on disk. The staged ops also remain
    in place so the caller can retry."""
    (client.resolve("a.txt").parent).mkdir(exist_ok=True)
    client.resolve("a.txt").write_bytes(b"a-original")
    client.resolve("b.txt").write_bytes(b"b-original")

    async def good_writer() -> bytes:
        return b"a-new"

    async def bad_writer() -> bytes:
        raise RuntimeError("simulated writer failure")

    t, _ = client.begin_staging()
    await client.stage_write("a.txt", good_writer)
    await client.stage_write("b.txt", bad_writer)

    with pytest.raises(RuntimeError, match="simulated writer failure"):
        await client.commit_staged()

    assert client.resolve("a.txt").read_bytes() == b"a-original"
    assert client.resolve("b.txt").read_bytes() == b"b-original"
    client.end_staging(t)


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

    t, _ = client.begin_staging()
    await client.stage_write("a.txt", b"a-new")
    await client.stage_write("b.txt", b_writer)

    with pytest.raises(RuntimeError, match="once"):
        await client.commit_staged()

    fail = False
    await client.commit_staged()
    client.end_staging(t)
    assert await client.read("a.txt") == b"a-new"
    assert await client.read("b.txt") == b"b-new"


async def test_commit_leaves_no_orphan_temp_directories(
    client: FileClient, tmp_path: Path
):
    """A successful commit cleans up its temp directory and any backup."""
    t, _ = client.begin_staging()
    await client.stage_write("file.txt", b"content")
    await client.commit_staged()
    client.end_staging(t)

    siblings = list(tmp_path.iterdir())
    session_name = client.resolve(".").resolve().name
    assert [s.name for s in siblings] == [session_name]


async def test_commit_retries_transient_permission_error_while_swapping_temp(
    client: FileClient, monkeypatch: pytest.MonkeyPatch
):
    t, _ = client.begin_staging()
    await client.stage_write("file.txt", b"old")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    await client.stage_write("file.txt", b"new")

    original_rename = os.rename
    rename_failures = 0

    def flaky_rename(source: str | Path, target: str | Path):
        nonlocal rename_failures
        source_path = Path(source)
        target_path = Path(target)
        if (
            rename_failures == 0
            and source_path.name.startswith("test-session" + _TEMP_PREFIX)
            and target_path.name == "test-session"
        ):
            rename_failures += 1
            raise PermissionError("simulated transient rename lock")
        return original_rename(source, target)

    monkeypatch.setattr(os, "rename", flaky_rename)

    await client.commit_staged()
    client.end_staging(t2)

    assert rename_failures == 1
    assert await client.read("file.txt") == b"new"


async def test_failed_commit_leaves_no_orphan_temp_directories(
    client: FileClient, tmp_path: Path
):
    """Even if a writer raises, the temp directory is cleaned up."""

    async def bad_writer() -> bytes:
        raise RuntimeError("nope")

    t, _ = client.begin_staging()
    await client.stage_write("file.txt", bad_writer)
    with pytest.raises(RuntimeError):
        await client.commit_staged()
    client.end_staging(t)

    session_name = client.resolve(".").resolve().name
    siblings = [s.name for s in tmp_path.iterdir()]
    assert all(
        name == session_name or not name.startswith(session_name + _TEMP_PREFIX)
        for name in siblings
    )
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


async def test_recovery_retries_transient_permission_error_restoring_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    session_id = "recoverable-retry"
    backup = tmp_path / (session_id + _BACKUP_SUFFIX)
    backup.mkdir()
    (backup / "saved.txt").write_bytes(b"saved")

    original_rename = os.rename
    rename_failures = 0

    def flaky_rename(source: str | Path, target: str | Path):
        nonlocal rename_failures
        source_path = Path(source)
        target_path = Path(target)
        if (
            rename_failures == 0
            and source_path.name == session_id + _BACKUP_SUFFIX
            and target_path.name == session_id
        ):
            rename_failures += 1
            raise PermissionError("simulated transient recovery lock")
        return original_rename(source, target)

    monkeypatch.setattr(os, "rename", flaky_rename)

    client = FileClient(base_dir=tmp_path, session_id=session_id)

    assert rename_failures == 1
    assert await client.read("saved.txt") == b"saved"
    assert not backup.exists()


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
    (tmp_path / (session_id + _TEMP_PREFIX + "abc123") / "junk.txt").write_bytes(b"")
    (tmp_path / (session_id + _TEMP_PREFIX + "def456")).mkdir()

    FileClient(base_dir=tmp_path, session_id=session_id)

    siblings = {s.name for s in tmp_path.iterdir()}
    assert siblings == {session_id}


async def test_stage_write_after_stage_delete_writes(client: FileClient):
    """A delete followed by a write on the same path should land as a write
    (the pending delete is cancelled by the subsequent write)."""
    path = "test.txt"
    (client.resolve(path).parent).mkdir(exist_ok=True)
    with open(client.resolve(path), "wb") as f:
        f.write(b"initial")

    t, _ = client.begin_staging()
    await client.stage_delete(path)
    await client.stage_write(path, b"new content")
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read(path) == b"new content"


async def test_read_observes_staged_write(client: FileClient):
    t, _ = client.begin_staging()
    await client.stage_write("k.txt", b"v")
    assert await client.read("k.txt") == b"v"
    client.end_staging(t)


async def test_staged_write_overrides_committed_file(client: FileClient):
    t, _ = client.begin_staging()
    await client.stage_write("k.txt", b"old")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    await client.stage_write("k.txt", b"new")
    assert await client.read("k.txt") == b"new"
    assert client.resolve("k.txt").read_bytes() == b"old"
    client.end_staging(t2)


async def test_read_observes_staged_delete(client: FileClient):
    t, _ = client.begin_staging()
    await client.stage_write("k.txt", b"v")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    await client.stage_delete("k.txt")
    assert await client.read("k.txt") is None
    client.end_staging(t2)


async def test_read_serves_callback_staged_write(client: FileClient):
    async def writer() -> bytes:
        return b"from-callback"

    t, _ = client.begin_staging()
    await client.stage_write("k.txt", writer)
    assert await client.read("k.txt") == b"from-callback"
    client.end_staging(t)


async def test_staged_read_reresolves_callback_each_time(client: FileClient):
    """A staged read invokes the callback fresh every time (results are not
    cached), so a callback that closes over mutable state always reflects the
    current state."""
    calls: list[int] = []

    async def writer() -> bytes:
        calls.append(1)
        return b"v"

    t, _ = client.begin_staging()
    await client.stage_write("k.txt", writer)
    assert await client.read("k.txt") == b"v"
    assert await client.read("k.txt") == b"v"
    assert calls == [1, 1]
    client.end_staging(t)


async def test_restaging_changes_staged_read(client: FileClient):
    t, _ = client.begin_staging()
    await client.stage_write("k.txt", b"first")
    assert await client.read("k.txt") == b"first"

    await client.stage_write("k.txt", b"second")
    assert await client.read("k.txt") == b"second"
    client.end_staging(t)


async def test_clear_staged_discards_staged_read(client: FileClient):
    t, _ = client.begin_staging()
    await client.stage_write("k.txt", b"v")
    assert await client.read("k.txt") == b"v"

    await client.clear_staged()
    assert await client.read("k.txt") is None
    client.end_staging(t)


async def test_read_falls_through_to_committed_file(client: FileClient):
    t, _ = client.begin_staging()
    await client.stage_write("k.txt", b"v")
    await client.commit_staged()
    client.end_staging(t)
    assert await client.read("k.txt") == b"v"


async def test_concurrent_staged_reads_both_resolve(client: FileClient):
    """Two concurrent staged reads of the same path each resolve the staged
    write callback and observe the staged value."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_writer() -> bytes:
        started.set()
        await release.wait()
        return b"staged"

    t, _ = client.begin_staging()
    await client.stage_write("k.txt", slow_writer)

    task_a = asyncio.create_task(client.read("k.txt"))
    await started.wait()

    task_b = asyncio.create_task(client.read("k.txt"))
    await asyncio.sleep(0)

    release.set()
    assert await task_b == b"staged"
    assert await task_a == b"staged"
    client.end_staging(t)


async def test_read_staged_false_returns_committed_ignoring_staged(
    client: FileClient,
):
    """staged=False reads only the committed file, ignoring a staged write or
    delete on the same path."""
    client.resolve("k.txt").write_bytes(b"committed")

    t, _ = client.begin_staging()
    await client.stage_write("k.txt", b"staged")
    assert await client.read("k.txt") == b"staged"
    assert await client.read("k.txt", staged=False) == b"committed"

    await client.stage_delete("k.txt")
    assert await client.read("k.txt") is None
    assert await client.read("k.txt", staged=False) == b"committed"
    client.end_staging(t)


async def test_commit_reflects_state_mutated_after_a_staged_read(
    client: FileClient,
):
    """A staged write callback may close over mutable state that keeps
    changing during the transaction (e.g. a store that buffers entries in
    memory and serializes them at commit). Reading mid-transaction must not
    freeze the value: a later mutation is still reflected by both subsequent
    reads and the eventual commit."""
    items: list[bytes] = [b"a"]

    async def writer() -> bytes:
        return b",".join(items)

    t, _ = client.begin_staging()
    await client.stage_write("k.txt", writer)

    assert await client.read("k.txt") == b"a"

    items.append(b"b")
    assert await client.read("k.txt") == b"a,b"

    await client.commit_staged()
    client.end_staging(t)
    assert await client.read("k.txt") == b"a,b"


async def test_callback_reads_committed_from_child_task(client: FileClient):
    """A write callback reads its own committed content with staged=False, so
    it works the same whether it reads inline or from a child task it spawns."""
    client.resolve("k.txt").write_bytes(b"committed")

    async def writer() -> bytes:
        async def read_self() -> bytes | None:
            return await client.read("k.txt", staged=False)

        val = await asyncio.create_task(read_self())
        return (val or b"") + b"-new"

    t, _ = client.begin_staging()
    await client.stage_write("k.txt", writer)
    assert await client.read("k.txt") == b"committed-new"
    client.end_staging(t)


async def test_stage_write_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        await client.stage_write("x.txt", b"data")


async def test_commit_staged_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        await client.commit_staged()


async def test_clear_staged_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        await client.clear_staged()


async def test_stage_delete_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        await client.stage_delete("x.txt")


async def test_file_client_parent_metadata_not_committed_by_child_transaction(
    tmp_path, serializer
):
    client = FileClient(base_dir=tmp_path, session_id="shared-file-client")
    store = JsonFileSessionStore(client=client, serializer=serializer)

    parent = ExecutionId(parent_id=None, name="parent", sequence=0, args_hash="p")
    child = ExecutionId(parent_id=parent, name="child", sequence=0, args_hash="c")

    parent_tx = await store.begin_transaction()
    await store.start_execution(parent)
    await client.stage_write("metadata/parent.json", b'{"state":"parent"}')

    child_tx = await store.begin_transaction()
    child_execution = await store.start_execution(child)
    await child_execution.complete("child", str)
    await child_tx.commit()

    child_record = await store.get_execution_record(child, str)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    assert await client.read("metadata/parent.json", staged=False) is None
    assert await client.read("metadata/parent.json") == b'{"state":"parent"}'

    await parent_tx.rollback()

    assert await client.read("metadata/parent.json") is None
    assert await client.read("metadata/parent.json", staged=False) is None
