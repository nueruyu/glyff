"""What a half-finished migration write leaves behind: nothing.

The shared contract covers a migrator that refuses. This is the failure only
this backend can stage — the document replacement itself going wrong.
"""

import json
from pathlib import Path

import pytest
from glyff import Execution, SessionId, TransactionScope
from glyff.migration import (
    MigrationReport,
    SessionMetadata,
    SessionMigrationResult,
    StoredSession,
)
from glyff.testing import canonical_arguments, make_execution_id
from glyff_file_store import JsonFileBackend
from glyff_file_store._file_client import _STORE_FILE, _TEMP_PREFIX

SESSION = SessionId("migrate")


class ReplacingMigrator:
    def __init__(self, *executions: Execution, app_version: str = "v2") -> None:
        self._executions = executions
        self._app_version = app_version

    def migrate(self, source: StoredSession) -> SessionMigrationResult:
        return SessionMigrationResult(
            session=StoredSession(
                metadata=SessionMetadata(app_version=self._app_version),
                executions=self._executions,
            ),
            report=MigrationReport(
                from_version=source.metadata.app_version, to_version=self._app_version
            ),
        )


def started(name: str) -> Execution:
    return Execution.start(make_execution_id(name), canonical_arguments())


async def seed(backend: JsonFileBackend, *names: str) -> list[Execution]:
    await backend.claim_session(SESSION, "v1")
    seeded = []
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

    monkeypatch.setattr(backend._client, "_replace_sync", refuse)

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
    assert await backend.claim_session(SESSION, "v-later") == "v1"


async def test_a_failed_replacement_strands_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    backend = JsonFileBackend(base_dir=tmp_path)
    await seed(backend, "before")

    def refuse(source: str, target: Path) -> None:
        raise OSError("refusing to replace")

    monkeypatch.setattr(backend._client, "_replace_sync", refuse)

    with pytest.raises(OSError):
        await backend.session_migration.run(
            SESSION, ReplacingMigrator(started("after"))
        )

    assert not list(tmp_path.glob(_TEMP_PREFIX + "*"))


async def test_a_migration_rewrites_the_session_in_place_in_the_document(
    tmp_path: Path,
):
    backend = JsonFileBackend(base_dir=tmp_path)
    await seed(backend, "before")
    after = started("after")

    await backend.session_migration.run(SESSION, ReplacingMigrator(after))

    document = json.loads((tmp_path / _STORE_FILE).read_text())
    session = document["sessions"][SESSION.value]
    assert session["app_version"] == "v2"
    assert [path.split("#")[0] for path in session["executions"]] == ["after"]
