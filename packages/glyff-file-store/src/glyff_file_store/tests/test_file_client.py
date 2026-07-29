import os
from pathlib import Path

import pytest
from glyff import Execution, ExecutionId, ExecutionStatus, SerializedValue

from glyff_file_store import FileExecutionRepository, FileTransactionProvider
from glyff_file_store._file_client import FileClient
from glyff_file_store._file_client import _BACKUP_SUFFIX, _TEMP_PREFIX


@pytest.fixture
def client(tmp_path: Path) -> FileClient:
    return FileClient(base_dir=tmp_path, session_id="test-session")


async def test_commit_single_write(client: FileClient):
    path = "test.txt"
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
    path = "test.txt"
    t, _ = client.begin_staging()
    client.stage_write(path, b"first")
    client.stage_write(path, b"second")
    await client.commit_staged()
    client.end_staging(t)
    assert await client.read(path) == b"second"


async def test_delete_cancels_staged_write(client: FileClient):
    path = "test.txt"
    (client.resolve(path).parent).mkdir(exist_ok=True)
    with open(client.resolve(path), "wb") as f:
        f.write(b"initial")

    t, _ = client.begin_staging()
    client.stage_write(path, b"new")
    client.stage_delete(path)
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read(path) is None


async def test_rollback_clears_staged_write(client: FileClient):
    path = "test.txt"
    t, _ = client.begin_staging()
    client.stage_write(path, b"a")
    await client.clear_staged()
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read(path) is None


async def test_commit_applies_writes_across_multiple_files(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("file1.txt", b"first-content")
    client.stage_write("file2.txt", b"second-content")
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read("file1.txt") == b"first-content"
    assert await client.read("file2.txt") == b"second-content"


async def test_stage_update_not_evaluated_at_stage_time(client: FileClient):
    path = "test.txt"
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
    path = "log.txt"

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
    path = "test.txt"
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
    (client.resolve("a.txt").parent).mkdir(exist_ok=True)
    client.resolve("a.txt").write_bytes(b"a-original")
    client.resolve("b.txt").write_bytes(b"b-original")

    def bad_fn(data: bytes | None) -> bytes | None:
        raise RuntimeError("simulated failure")

    t, _ = client.begin_staging()
    client.stage_update("a.txt", lambda data: b"a-new")
    client.stage_update("b.txt", bad_fn)

    with pytest.raises(RuntimeError, match="simulated failure"):
        await client.commit_staged()

    assert client.resolve("a.txt").read_bytes() == b"a-original"
    assert client.resolve("b.txt").read_bytes() == b"b-original"
    client.end_staging(t)


async def test_partial_commit_failure_can_be_retried(client: FileClient):
    (client.resolve("a.txt").parent).mkdir(exist_ok=True)
    client.resolve("a.txt").write_bytes(b"a-original")

    fail = True

    def b_fn(data: bytes | None) -> bytes | None:
        if fail:
            raise RuntimeError("once")
        return b"b-new"

    t, _ = client.begin_staging()
    client.stage_write("a.txt", b"a-new")
    client.stage_update("b.txt", b_fn)

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
    t, _ = client.begin_staging()
    client.stage_write("file.txt", b"content")
    await client.commit_staged()
    client.end_staging(t)

    siblings = list(tmp_path.iterdir())
    session_name = client.resolve(".").resolve().name
    assert [s.name for s in siblings] == [session_name]


async def test_commit_retries_transient_permission_error_while_swapping_temp(
    client: FileClient, monkeypatch: pytest.MonkeyPatch
):
    t, _ = client.begin_staging()
    client.stage_write("file.txt", b"old")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_write("file.txt", b"new")

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
    t, _ = client.begin_staging()
    client.stage_update(
        "file.txt", lambda data: (_ for _ in ()).throw(RuntimeError("nope"))
    )
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
    session_id = "with-stale-bak"
    (tmp_path / session_id).mkdir()
    (tmp_path / session_id / "live.txt").write_bytes(b"live")
    (tmp_path / (session_id + _BACKUP_SUFFIX)).mkdir()
    (tmp_path / (session_id + _BACKUP_SUFFIX) / "stale.txt").write_bytes(b"stale")

    client = FileClient(base_dir=tmp_path, session_id=session_id)
    assert await client.read("live.txt") == b"live"
    assert not (tmp_path / (session_id + _BACKUP_SUFFIX)).exists()


async def test_recovery_cleans_orphan_temp_directories(tmp_path: Path):
    session_id = "with-orphan-temps"
    (tmp_path / session_id).mkdir()
    (tmp_path / (session_id + _TEMP_PREFIX + "abc123")).mkdir()
    (tmp_path / (session_id + _TEMP_PREFIX + "abc123") / "junk.txt").write_bytes(b"")
    (tmp_path / (session_id + _TEMP_PREFIX + "def456")).mkdir()

    FileClient(base_dir=tmp_path, session_id=session_id)

    siblings = {s.name for s in tmp_path.iterdir()}
    assert siblings == {session_id}


async def test_stage_write_after_stage_delete_writes(client: FileClient):
    path = "test.txt"
    (client.resolve(path).parent).mkdir(exist_ok=True)
    with open(client.resolve(path), "wb") as f:
        f.write(b"initial")

    t, _ = client.begin_staging()
    client.stage_delete(path)
    client.stage_write(path, b"new content")
    await client.commit_staged()
    client.end_staging(t)

    assert await client.read(path) == b"new content"


async def test_read_observes_staged_write(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("k.txt", b"v")
    assert await client.read("k.txt") == b"v"
    client.end_staging(t)


async def test_staged_write_overrides_committed_file(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("k.txt", b"old")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_write("k.txt", b"new")
    assert await client.read("k.txt") == b"new"
    assert client.resolve("k.txt").read_bytes() == b"old"
    client.end_staging(t2)


async def test_read_observes_staged_delete(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("k.txt", b"v")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_delete("k.txt")
    assert await client.read("k.txt") is None
    client.end_staging(t2)


async def test_read_observes_staged_update(client: FileClient):
    client.resolve("k.txt").write_bytes(b"committed")

    t, _ = client.begin_staging()
    client.stage_update("k.txt", lambda data: (data or b"") + b"-updated")
    assert await client.read("k.txt") == b"committed-updated"
    client.end_staging(t)


async def test_staged_update_can_create_file(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_update("k.txt", lambda data: b"created")
    assert await client.read("k.txt") == b"created"
    client.end_staging(t)


async def test_restaging_changes_staged_read(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("k.txt", b"first")
    assert await client.read("k.txt") == b"first"

    client.stage_write("k.txt", b"second")
    assert await client.read("k.txt") == b"second"
    client.end_staging(t)


async def test_clear_staged_discards_staged_read(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("k.txt", b"v")
    assert await client.read("k.txt") == b"v"

    await client.clear_staged()
    assert await client.read("k.txt") is None
    client.end_staging(t)


async def test_read_falls_through_to_committed_file(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("k.txt", b"v")
    await client.commit_staged()
    client.end_staging(t)
    assert await client.read("k.txt") == b"v"


async def test_read_staged_false_returns_committed_ignoring_staged(
    client: FileClient,
):
    client.resolve("k.txt").write_bytes(b"committed")

    t, _ = client.begin_staging()
    client.stage_write("k.txt", b"staged")
    assert await client.read("k.txt") == b"staged"
    assert await client.read("k.txt", staged=False) == b"committed"

    client.stage_delete("k.txt")
    assert await client.read("k.txt") is None
    assert await client.read("k.txt", staged=False) == b"committed"
    client.end_staging(t)


async def test_stage_update_reflects_mutated_state_at_commit(client: FileClient):
    items: list[bytes] = [b"a"]

    def fn(data: bytes | None) -> bytes | None:
        return b",".join(items)

    t, _ = client.begin_staging()
    client.stage_update("k.txt", fn)

    assert await client.read("k.txt") == b"a"

    items.append(b"b")
    assert await client.read("k.txt") == b"a,b"

    await client.commit_staged()
    client.end_staging(t)
    assert await client.read("k.txt") == b"a,b"


async def test_stage_write_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_write("x.txt", b"data")


async def test_commit_staged_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        await client.commit_staged()


async def test_clear_staged_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        await client.clear_staged()


async def test_stage_delete_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_delete("x.txt")


async def test_stage_update_outside_transaction_raises(client: FileClient):
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_update("x.txt", lambda data: data)


async def test_list_keys_returns_committed_files(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("a.txt", b"1")
    client.stage_write("b.txt", b"2")
    await client.commit_staged()
    client.end_staging(t)

    keys = await client.list_keys()
    assert keys == {"a.txt", "b.txt"}


async def test_list_keys_with_prefix(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("exec/a.txt", b"1")
    client.stage_write("exec/b.txt", b"2")
    client.stage_write("other/c.txt", b"3")
    await client.commit_staged()
    client.end_staging(t)

    keys = await client.list_keys(prefix="exec/")
    assert keys == {"exec/a.txt", "exec/b.txt"}


async def test_list_keys_respects_staged_writes(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("a.txt", b"1")
    assert await client.list_keys() == {"a.txt"}
    client.end_staging(t)


async def test_list_keys_respects_staged_deletes(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("a.txt", b"1")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_delete("a.txt")
    keys = await client.list_keys()
    assert keys == set()
    client.end_staging(t2)


async def test_list_keys_respects_staged_updates(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("a.txt", b"1")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_update("a.txt", lambda data: b"2")
    keys = await client.list_keys()
    assert keys == {"a.txt"}
    client.end_staging(t2)


async def test_list_keys_respects_staged_update_that_deletes(client: FileClient):
    t, _ = client.begin_staging()
    client.stage_write("a.txt", b"1")
    await client.commit_staged()
    client.end_staging(t)

    t2, _ = client.begin_staging()
    client.stage_update("a.txt", lambda data: None)
    keys = await client.list_keys()
    assert keys == set()
    client.end_staging(t2)


async def test_list_keys_ignores_staged_writes_when_staged_false(
    client: FileClient,
):
    t, _ = client.begin_staging()
    client.stage_write("a.txt", b"staged")
    keys = await client.list_keys(staged=False)
    assert keys == set()
    client.end_staging(t)


async def test_file_client_parent_metadata_not_committed_by_child_transaction(
    tmp_path, serializer
):
    client = FileClient(base_dir=tmp_path, session_id="shared-file-client")
    repository = FileExecutionRepository(client)
    transaction_provider = FileTransactionProvider(client)

    parent = ExecutionId(parent_id=None, name="parent", sequence=0, args_hash="p")
    child = ExecutionId(parent_id=parent, name="child", sequence=0, args_hash="c")

    parent_tx = await transaction_provider.begin_transaction()
    await repository.save(Execution.start(parent, SerializedValue(b"{}")))

    def parent_meta(data: bytes | None) -> bytes | None:
        return b'{"state":"parent"}'

    client.stage_update("metadata/parent.json", parent_meta)

    child_tx = await transaction_provider.begin_transaction()
    child_execution = Execution.start(child, SerializedValue(b"{}"))
    child_execution.complete(SerializedValue(await serializer.serialize("child", str)))
    await repository.save(child_execution)
    await child_tx.commit()

    child_record = await repository.get(child)
    assert child_record is not None
    assert child_record.status == ExecutionStatus.COMPLETED

    assert await client.read("metadata/parent.json", staged=False) is None
    assert await client.read("metadata/parent.json") == b'{"state":"parent"}'

    await parent_tx.rollback()

    assert await client.read("metadata/parent.json") is None
    assert await client.read("metadata/parent.json", staged=False) is None
