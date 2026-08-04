import os
from pathlib import Path

import pytest
from glyff import Execution, ExecutionStatus, SerializedValue, SessionId
from glyff.testing import canonical_arguments, make_execution_id

from glyff_file_store import FileExecutionRepository, FileTransactionProvider
from glyff_file_store._file_client import FileClient
from glyff_file_store._file_client import _BACKUP_PREFIX, _TEMP_PREFIX


SESSION = "test-session"


def KEY(path: str) -> tuple[str, str]:
    return (SESSION, path)


def _session_dirs(base_dir: Path) -> list[str]:
    """Session directories only: the store keeps its own files dot-prefixed."""
    return sorted(p.name for p in base_dir.iterdir() if not p.name.startswith("."))


def _write_committed(client: FileClient, path: str, data: bytes) -> None:
    target = client.resolve(KEY(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


@pytest.fixture
def client(tmp_path: Path) -> FileClient:
    return FileClient(base_dir=tmp_path)


async def test_commit_single_write(client: FileClient):
    path = KEY("test.txt")
    t1, _ = client.begin_staging()
    client.stage_write(path, b"hello")
    await client.commit_staged()
    client.end_staging(t1)
    assert await client.read(path) == b"hello"

    t2, _ = client.begin_staging()
    client.stage_write(path, b"world")
    await client.commit_staged()
    client.end_staging(t2)
    assert await client.read(path) == b"world"


async def test_staging_same_path_last_write_wins(client: FileClient):
    path = KEY("test.txt")
    t, _ = client.begin_staging()
    client.stage_write(path, b"first")
    client.stage_write(path, b"second")
    await client.commit_staged()
    client.end_staging(t)
    assert await client.read(path) == b"second"


async def test_delete_cancels_staged_write(client: FileClient):
    path = KEY("test.txt")
    _write_committed(client, path[1], b"initial")

    t, _ = client.begin_staging()
    client.stage_write(path, b"new")
    client.stage_delete(path)
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read(path) is None


async def test_rollback_clears_staged_write(client: FileClient):
    path = KEY("test.txt")
    t, _ = client.begin_staging()
    client.stage_write(path, b"a")
    await client.clear_staged()
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read(path) is None


async def test_commit_applies_writes_across_multiple_files(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write(KEY("file1.txt"), b"first-content")
    client.stage_write(KEY("file2.txt"), b"second-content")
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read(KEY("file1.txt")) == b"first-content"
    assert await client.read(KEY("file2.txt")) == b"second-content"


async def test_stage_update_not_evaluated_at_stage_time(client: FileClient):
    path = KEY("test.txt")
    calls = 0

    def fn(data: bytes | None) -> bytes | None:
        nonlocal calls
        calls += 1
        return b"called"

    t, _ = client.begin_staging()
    client.stage_update(path, fn)
    assert calls == 0
    await client.commit_staged()
    client.end_staging(t)
    assert calls == 1
    assert await client.read(path) == b"called"


async def test_stage_update_can_read_committed_value(client: FileClient):
    path = KEY("log.txt")

    t1, _ = client.begin_staging()
    client.stage_write(path, b"first\n")
    await client.commit_staged()
    client.end_staging(t1)

    t2, _ = client.begin_staging()
    client.stage_update(path, lambda data: (data or b"") + b"second\n")
    await client.commit_staged()
    client.end_staging(t2)
    assert await client.read(path) == b"first\nsecond\n"

    t3, _ = client.begin_staging()
    client.stage_update(path, lambda data: (data or b"") + b"third\n")
    await client.commit_staged()
    client.end_staging(t3)
    assert await client.read(path) == b"first\nsecond\nthird\n"


async def test_stage_update_can_delete_file(client: FileClient):
    path = KEY("test.txt")
    t, _ = client.begin_staging()
    client.stage_write(path, b"content")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_update(path, lambda data: None)
    await client.commit_staged()
    client.end_staging(t2)
    assert await client.read(path) is None


async def test_partial_commit_failure_leaves_disk_unchanged(client: FileClient):
    _write_committed(client, "a.txt", b"a-original")
    _write_committed(client, "b.txt", b"b-original")

    def bad_fn(data: bytes | None) -> bytes | None:
        raise RuntimeError("simulated failure")

    t, _ = client.begin_staging()
    client.stage_update(KEY("a.txt"), lambda data: b"a-new")
    client.stage_update(KEY("b.txt"), bad_fn)

    with pytest.raises(RuntimeError, match="simulated failure"):
        await client.commit_staged()

    assert client.resolve(KEY("a.txt")).read_bytes() == b"a-original"
    assert client.resolve(KEY("b.txt")).read_bytes() == b"b-original"
    client.end_staging(t)


async def test_partial_commit_failure_can_be_retried(client: FileClient):
    (client.resolve(KEY("a.txt")).parent).mkdir(exist_ok=True)
    client.resolve(KEY("a.txt")).write_bytes(b"a-original")

    fail = True

    def b_fn(data: bytes | None) -> bytes | None:
        if fail:
            raise RuntimeError("once")
        return b"b-new"

    t, _ = client.begin_staging()
    client.stage_write(KEY("a.txt"), b"a-new")
    client.stage_update(KEY("b.txt"), b_fn)

    with pytest.raises(RuntimeError, match="once"):
        await client.commit_staged()

    fail = False
    await client.commit_staged()
    client.end_staging(t)
    assert await client.read(KEY("a.txt")) == b"a-new"
    assert await client.read(KEY("b.txt")) == b"b-new"


async def test_commit_leaves_no_orphan_temp_directories(
    client: FileClient, tmp_path: Path
):
    t, _ = client.begin_staging()
    client.stage_write(KEY("file.txt"), b"content")
    await client.commit_staged()
    client.end_staging(t)

    assert _session_dirs(tmp_path) == [SESSION]


async def test_commit_retries_transient_permission_error_while_swapping_temp(
    client: FileClient, monkeypatch: pytest.MonkeyPatch
):
    t, _ = client.begin_staging()
    client.stage_write(KEY("file.txt"), b"old")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_write(KEY("file.txt"), b"new")

    original_rename = os.rename
    rename_failures = 0

    def flaky_rename(source: str | Path, target: str | Path):
        nonlocal rename_failures
        source_path = Path(source)
        target_path = Path(target)
        if (
            rename_failures == 0
            and source_path.name.startswith(_TEMP_PREFIX)
            and target_path.name == SESSION
        ):
            rename_failures += 1
            raise PermissionError("simulated transient rename lock")
        return original_rename(source, target)

    monkeypatch.setattr(os, "rename", flaky_rename)

    await client.commit_staged()
    client.end_staging(t2)

    assert rename_failures == 1
    assert await client.read(KEY("file.txt")) == b"new"


async def test_failed_commit_leaves_no_orphan_temp_directories(
    client: FileClient, tmp_path: Path
):
    t, _ = client.begin_staging()
    client.stage_update(
        KEY("file.txt"), lambda data: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    with pytest.raises(RuntimeError):
        await client.commit_staged()
    client.end_staging(t)

    assert _session_dirs(tmp_path) == []
    assert not (tmp_path / (_BACKUP_PREFIX + SESSION)).exists()


async def test_recovery_restores_session_from_orphan_backup(tmp_path: Path):
    session_id = "recoverable"
    (tmp_path / (_BACKUP_PREFIX + session_id)).mkdir()
    (tmp_path / (_BACKUP_PREFIX + session_id) / "saved.txt").write_bytes(b"saved")

    client = FileClient(base_dir=tmp_path)
    assert await client.read((session_id, "saved.txt")) == b"saved"
    assert not (tmp_path / (_BACKUP_PREFIX + session_id)).exists()


async def test_recovery_retries_transient_permission_error_restoring_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    session_id = "recoverable-retry"
    backup = tmp_path / (_BACKUP_PREFIX + session_id)
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
            and source_path.name == _BACKUP_PREFIX + session_id
            and target_path.name == session_id
        ):
            rename_failures += 1
            raise PermissionError("simulated transient recovery lock")
        return original_rename(source, target)

    monkeypatch.setattr(os, "rename", flaky_rename)

    client = FileClient(base_dir=tmp_path)

    assert rename_failures == 1
    assert await client.read((session_id, "saved.txt")) == b"saved"
    assert not backup.exists()


async def test_recovery_drops_orphan_backup_when_session_present(tmp_path: Path):
    session_id = "with-stale-bak"
    (tmp_path / session_id).mkdir()
    (tmp_path / session_id / "live.txt").write_bytes(b"live")
    (tmp_path / (_BACKUP_PREFIX + session_id)).mkdir()
    (tmp_path / (_BACKUP_PREFIX + session_id) / "stale.txt").write_bytes(b"stale")

    client = FileClient(base_dir=tmp_path)
    assert await client.read((session_id, "live.txt")) == b"live"
    assert not (tmp_path / (_BACKUP_PREFIX + session_id)).exists()


async def test_recovery_cleans_orphan_temp_directories(tmp_path: Path):
    session_id = "with-orphan-temps"
    (tmp_path / session_id).mkdir()
    (tmp_path / (_TEMP_PREFIX + "abc123")).mkdir()
    (tmp_path / (_TEMP_PREFIX + "abc123") / "junk.txt").write_bytes(b"")
    (tmp_path / (_TEMP_PREFIX + "def456")).mkdir()

    FileClient(base_dir=tmp_path)

    assert _session_dirs(tmp_path) == [session_id]


async def test_stage_write_after_stage_delete_writes(client: FileClient):
    path = KEY("test.txt")
    _write_committed(client, path[1], b"initial")

    t, _ = client.begin_staging()
    client.stage_delete(path)
    client.stage_write(path, b"new content")
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read(path) == b"new content"


async def test_read_observes_staged_write(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write(KEY("k.txt"), b"v")
    assert await client.read(KEY("k.txt")) == b"v"
    client.end_staging(t)


async def test_staged_write_overrides_committed_file(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write(KEY("k.txt"), b"old")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_write(KEY("k.txt"), b"new")
    assert await client.read(KEY("k.txt")) == b"new"
    assert client.resolve(KEY("k.txt")).read_bytes() == b"old"
    client.end_staging(t2)


async def test_read_observes_staged_delete(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write(KEY("k.txt"), b"v")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_delete(KEY("k.txt"))
    assert await client.read(KEY("k.txt")) is None
    client.end_staging(t2)


async def test_read_observes_staged_update(client: FileClient):
    _write_committed(client, "k.txt", b"committed")

    t, _ = client.begin_staging()
    client.stage_update(KEY("k.txt"), lambda data: (data or b"") + b"-updated")
    assert await client.read(KEY("k.txt")) == b"committed-updated"
    client.end_staging(t)


async def test_staged_update_can_create_file(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_update(KEY("k.txt"), lambda data: b"created")
    assert await client.read(KEY("k.txt")) == b"created"
    client.end_staging(t)


async def test_restaging_changes_staged_read(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write(KEY("k.txt"), b"first")
    assert await client.read(KEY("k.txt")) == b"first"

    client.stage_write(KEY("k.txt"), b"second")
    assert await client.read(KEY("k.txt")) == b"second"
    client.end_staging(t)


async def test_clear_staged_discards_staged_read(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write(KEY("k.txt"), b"v")
    assert await client.read(KEY("k.txt")) == b"v"

    await client.clear_staged()
    assert await client.read(KEY("k.txt")) is None
    client.end_staging(t)


async def test_read_falls_through_to_committed_file(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write(KEY("k.txt"), b"v")
    await client.commit_staged()
    client.end_staging(t)
    assert await client.read(KEY("k.txt")) == b"v"


async def test_read_staged_false_returns_committed_ignoring_staged(
    client: FileClient,
):
    _write_committed(client, "k.txt", b"committed")

    t, _ = client.begin_staging()
    client.stage_write(KEY("k.txt"), b"staged")
    assert await client.read(KEY("k.txt")) == b"staged"
    assert await client.read(KEY("k.txt"), staged=False) == b"committed"

    client.stage_delete(KEY("k.txt"))
    assert await client.read(KEY("k.txt")) is None
    assert await client.read(KEY("k.txt"), staged=False) == b"committed"
    client.end_staging(t)


async def test_stage_update_reflects_mutated_state_at_commit(client: FileClient):
    items: list[bytes] = [b"a"]

    def fn(data: bytes | None) -> bytes | None:
        return b",".join(items)

    t, _ = client.begin_staging()
    client.stage_update(KEY("k.txt"), fn)

    assert await client.read(KEY("k.txt")) == b"a"

    items.append(b"b")
    assert await client.read(KEY("k.txt")) == b"a,b"

    await client.commit_staged()
    client.end_staging(t)
    assert await client.read(KEY("k.txt")) == b"a,b"


async def test_stage_write_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_write(KEY("x.txt"), b"data")


async def test_commit_staged_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        await client.commit_staged()


async def test_clear_staged_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        await client.clear_staged()


async def test_stage_delete_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_delete(KEY("x.txt"))


async def test_stage_update_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_update(KEY("x.txt"), lambda data: data)


async def test_file_client_parent_metadata_not_committed_by_child_transaction(
    tmp_path, serializer
):
    client = FileClient(base_dir=tmp_path / "shared-file-client")
    repository = FileExecutionRepository(client)
    transaction_provider = FileTransactionProvider(client)

    parent = make_execution_id("parent")
    child = make_execution_id("child", parent=parent)

    parent_tx = await transaction_provider.begin_transaction()
    await repository.save(
        SessionId(SESSION), Execution.start(parent, canonical_arguments())
    )

    def parent_meta(data: bytes | None) -> bytes | None:
        return b'{"state":"parent"}'

    client.stage_update(KEY("metadata/parent.json"), parent_meta)

    child_tx = await transaction_provider.begin_transaction()
    child_execution = Execution.start(child, canonical_arguments())
    child_execution.complete(SerializedValue(await serializer.serialize("child", str)))
    await repository.save(SessionId(SESSION), child_execution)
    await child_tx.commit()

    child_record = await repository.get(SessionId(SESSION), child)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    assert await client.read(KEY("metadata/parent.json"), staged=False) is None
    assert await client.read(KEY("metadata/parent.json")) == b'{"state":"parent"}'

    await parent_tx.rollback()

    assert await client.read(KEY("metadata/parent.json")) is None
    assert await client.read(KEY("metadata/parent.json"), staged=False) is None
