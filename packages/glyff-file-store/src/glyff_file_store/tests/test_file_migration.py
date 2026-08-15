"""What a half-finished migration write leaves behind: nothing.

The shared contract covers a migrator that refuses. This is the failure only
this backend can stage — the document replacement itself going wrong.
"""

import asyncio
import json
import threading
from pathlib import Path

import pytest
from glyff import (
    DomainId,
    DomainVersion,
    DomainVersionMap,
    Execution,
    SessionId,
    TransactionScope,
)
from glyff.migration import (
    SessionMetadata,
    SessionMigrator,
    StoredSession,
)
from glyff.store.utils import path_to_execution_id
from glyff.testing import canonical_arguments, make_execution_id
from glyff_file_store import JsonFileBackend
from glyff_file_store._file_client import (
    _STORE_FILE,  # pyright: ignore[reportPrivateUsage]
    _TEMP_PREFIX,  # pyright: ignore[reportPrivateUsage]
)

SESSION = SessionId("migrate")
DOMAIN = DomainId("test")


class ReplacingMigrator(SessionMigrator):
    def __init__(self, *executions: Execution, version: str = "v2") -> None:
        self._executions = executions
        self._version = version

    def migrate(self, source: StoredSession) -> StoredSession:
        return StoredSession(
            metadata=SessionMetadata(DomainVersionMap({DOMAIN: self._version})),
            executions=self._executions,
        )


def started(name: str) -> Execution:
    return Execution.start(
        make_execution_id(name, domain_id=DOMAIN), canonical_arguments()
    )


async def _save(backend: JsonFileBackend, execution: Execution) -> None:
    async with TransactionScope(backend.transaction_provider):
        await backend.repository.save(SESSION, execution)


async def seed(backend: JsonFileBackend, *names: str) -> list[Execution]:
    await backend.claim_domain(SESSION, DOMAIN, DomainVersion("v1"))
    seeded: list[Execution] = []
    for name in names:
        execution = started(name)
        async with TransactionScope(backend.transaction_provider):
            await backend.repository.save(SESSION, execution)
        seeded.append(execution)
    return seeded


async def test_a_failed_replacement_changes_neither_half(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    backend = JsonFileBackend(base_dir=tmp_path)
    seeded = await seed(backend, "before")
    before = (tmp_path / _STORE_FILE).read_text()

    def refuse(source: str, target: Path) -> None:
        raise OSError("refusing to replace")

    monkeypatch.setattr(backend._client, "_replace_sync", refuse)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(OSError, match="refusing to replace"):
        await backend.session_migration.run(
            SESSION, ReplacingMigrator(started("after"))
        )

    monkeypatch.undo()
    # One document carries both halves, so a replacement that never lands leaves
    # the executions and the recorded version exactly as they were.
    assert (tmp_path / _STORE_FILE).read_text() == before
    assert [e.id async for e in backend.repository.executions(SESSION)] == [
        e.id for e in seeded
    ]
    assert (
        await backend.claim_domain(SESSION, DOMAIN, DomainVersion("v-later"))
    ).value == "v1"


async def test_a_failed_replacement_strands_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    backend = JsonFileBackend(base_dir=tmp_path)
    await seed(backend, "before")

    def refuse(source: str, target: Path) -> None:
        raise OSError("refusing to replace")

    monkeypatch.setattr(backend._client, "_replace_sync", refuse)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(OSError):
        await backend.session_migration.run(
            SESSION, ReplacingMigrator(started("after"))
        )

    assert not list(tmp_path.glob(_TEMP_PREFIX + "*"))


async def test_a_cancelled_migration_does_not_hand_the_store_on_early(
    tmp_path: Path,
):
    # Cancelling the caller does not stop the worker thread. If the lock went
    # with the cancellation, the next writer would take it, write, and then be
    # overwritten by this migration's replacement of a document read before it.
    migrating = JsonFileBackend(base_dir=tmp_path)
    writing = JsonFileBackend(base_dir=tmp_path)
    await seed(migrating, "before")
    after = started("after")
    intruder = started("intruder")

    inside = threading.Event()
    release = threading.Event()

    class SlowMigrator(ReplacingMigrator):
        def migrate(self, source: StoredSession) -> StoredSession:
            inside.set()
            release.wait(5)
            return super().migrate(source)

    migration = asyncio.create_task(
        migrating.session_migration.run(SESSION, SlowMigrator(after))
    )
    await asyncio.to_thread(inside.wait, 5)

    migration.cancel()
    await asyncio.sleep(0.05)
    writer = asyncio.create_task(
        _save(writing, intruder), name="writer-after-cancellation"
    )
    await asyncio.sleep(0.05)
    assert not writer.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await migration
    await writer

    # The intruder went second, so it is the one still standing.
    assert {e.id async for e in writing.repository.executions(SESSION)} == {
        after.id,
        intruder.id,
    }


async def test_repeated_cancellation_still_does_not_hand_the_store_on_early(
    tmp_path: Path,
):
    # Absorbing one cancellation is not enough: a second one would land in the
    # wait the first one put us in, and escape the lock with the worker still
    # holding the document.
    migrating = JsonFileBackend(base_dir=tmp_path)
    writing = JsonFileBackend(base_dir=tmp_path)
    await seed(migrating, "before")
    after = started("after")
    intruder = started("intruder")

    inside = threading.Event()
    release = threading.Event()

    class SlowMigrator(ReplacingMigrator):
        def migrate(self, source: StoredSession) -> StoredSession:
            inside.set()
            release.wait(5)
            return super().migrate(source)

    migration = asyncio.create_task(
        migrating.session_migration.run(SESSION, SlowMigrator(after))
    )
    await asyncio.to_thread(inside.wait, 5)

    for _ in range(3):
        migration.cancel()
        await asyncio.sleep(0.02)

    writer = asyncio.create_task(_save(writing, intruder))
    await asyncio.sleep(0.05)
    assert not writer.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await migration
    await writer

    assert {e.id async for e in writing.repository.executions(SESSION)} == {
        after.id,
        intruder.id,
    }


async def test_a_cancelled_migration_still_reports_a_worker_failure_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The worker's own failure is collected rather than left unretrieved, but
    # cancellation is what the caller asked for and what it is told.
    backend = JsonFileBackend(base_dir=tmp_path)
    await seed(backend, "before")

    inside = threading.Event()
    release = threading.Event()

    def refuse(source: str, target: Path) -> None:
        raise OSError("refusing to replace")

    monkeypatch.setattr(backend._client, "_replace_sync", refuse)  # pyright: ignore[reportPrivateUsage]

    class SlowMigrator(ReplacingMigrator):
        def migrate(self, source: StoredSession) -> StoredSession:
            inside.set()
            release.wait(5)
            return super().migrate(source)

    migration = asyncio.create_task(
        backend.session_migration.run(SESSION, SlowMigrator(started("after")))
    )
    await asyncio.to_thread(inside.wait, 5)
    migration.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await migration


async def test_a_migration_rewrites_the_session_in_place_in_the_document(
    tmp_path: Path,
):
    backend = JsonFileBackend(base_dir=tmp_path)
    await seed(backend, "before")
    after = started("after")

    await backend.session_migration.run(SESSION, ReplacingMigrator(after))

    document = json.loads((tmp_path / _STORE_FILE).read_text())
    session = document["sessions"][SESSION.value]
    assert session["domain_versions"] == {DOMAIN.value: "v2"}
    assert [
        path_to_execution_id(path).name.value for path in session["executions"]
    ] == ["after"]
