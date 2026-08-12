"""pytest conformance contract for backends offering session migration.

Re-exported from :mod:`glyff.testing`, the public entry point.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import replace

import pytest

from glyff import (
    Backend,
    DomainId,
    DomainVersion,
    Execution,
    SessionId,
    TransactionScope,
)
from glyff.exceptions import MigrationCollisionError, MigrationError
from glyff.migration import (
    MigratableBackend,
    MigrationReport,
    SessionMetadata,
    SessionMigrator,
    StoredSession,
)

from ._backend_contract import (
    DOMAIN,
    canonical_arguments,
    make_execution_id,
    save_execution,
    serialized_value,
)


MigratableBackendFactory = Callable[[str], MigratableBackend]

SESSION = SessionId("migrate")
OTHER_SESSION = SessionId("migrate-other")
DROPPED_DOMAIN = DomainId("migrate.dropped")


class RecordingMigrator(SessionMigrator):
    """Stands in for the runner: keeps what it was handed, returns what it was
    told to, and never touches a store."""

    def __init__(
        self,
        *,
        domain_versions: dict[DomainId, str] | None = None,
        executions: tuple[Execution, ...] | None = None,
        transform: Callable[[StoredSession], StoredSession] | None = None,
        raises: BaseException | None = None,
        before_return: Callable[[], None] | None = None,
    ) -> None:
        self._domain_versions = (
            {DOMAIN: "v2"} if domain_versions is None else domain_versions
        )
        self._executions = executions
        self._transform = transform
        self._raises = raises
        self._before_return = before_return
        self.source: StoredSession | None = None

    def migrate(self, source: StoredSession) -> StoredSession:
        self.source = source
        if self._raises is not None:
            raise self._raises
        if self._before_return is not None:
            self._before_return()

        if self._transform is not None:
            session = self._transform(source)
        else:
            session = StoredSession(
                metadata=SessionMetadata.from_strings(self._domain_versions),
                executions=(
                    source.executions if self._executions is None else self._executions
                ),
            )
        return session


async def _executions(
    backend: Backend, session_id: SessionId = SESSION
) -> list[Execution]:
    return [execution async for execution in backend.repository.executions(session_id)]


async def _claim_domain(
    backend: Backend, session_id: SessionId, domain_id: DomainId, version: str
) -> DomainVersion:
    return await backend.claim_domain(session_id, domain_id, DomainVersion(version))


class SessionMigrationContract:
    """Replacing one session's metadata and executions as a single fact."""

    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def backend_factory(self) -> MigratableBackendFactory:
        raise NotImplementedError

    async def _seeded(
        self, backend: MigratableBackend, *names: str, version: str = "v1"
    ) -> list[Execution]:
        await _claim_domain(backend, SESSION, DOMAIN, version)
        seeded = []
        for name in names:
            execution = Execution.start(make_execution_id(name), canonical_arguments())
            await save_execution(backend, execution, SESSION)
            seeded.append(execution)
        return seeded

    # -- What the migrator is handed -----------------------------------------

    async def test_the_source_carries_the_recorded_version_and_executions(
        self, backend_factory: MigratableBackendFactory
    ):
        backend = backend_factory("source")
        seeded = await self._seeded(backend, "first", "second")
        migrator = RecordingMigrator()

        await backend.session_migration.run(SESSION, migrator)

        assert migrator.source is not None
        assert migrator.source.metadata == SessionMetadata.from_strings({DOMAIN: "v1"})
        assert list(migrator.source.executions) == seeded

    async def test_the_source_holds_only_the_named_session(
        self, backend_factory: MigratableBackendFactory
    ):
        backend = backend_factory("source-scope")
        await self._seeded(backend, "mine")
        await _claim_domain(backend, OTHER_SESSION, DOMAIN, "v1")
        await save_execution(
            backend,
            Execution.start(make_execution_id("theirs"), canonical_arguments()),
            OTHER_SESSION,
        )
        migrator = RecordingMigrator()

        await backend.session_migration.run(SESSION, migrator)

        assert migrator.source is not None
        assert [e.id.name.value for e in migrator.source.executions] == ["mine"]

    async def test_an_unclaimed_session_is_refused(
        self, backend_factory: MigratableBackendFactory
    ):
        backend = backend_factory("source-unclaimed")

        with pytest.raises(MigrationError):
            await backend.session_migration.run(SESSION, RecordingMigrator())

    # -- What gets stored ----------------------------------------------------

    async def test_metadata_and_executions_are_replaced_together(
        self, backend_factory: MigratableBackendFactory
    ):
        backend = backend_factory("replace")
        await self._seeded(backend, "before")
        after = Execution.start(make_execution_id("after"), canonical_arguments())

        report = await backend.session_migration.run(
            SESSION,
            RecordingMigrator(domain_versions={DOMAIN: "v2"}, executions=(after,)),
        )

        assert report == MigrationReport.from_strings(
            source_domain_versions={DOMAIN: "v1"},
            target_domain_versions={DOMAIN: "v2"},
        )
        assert [e.id for e in await _executions(backend)] == [after.id]
        assert await _claim_domain(
            backend, SESSION, DOMAIN, "v-later"
        ) == DomainVersion("v2")

    async def test_executions_the_migrator_dropped_are_gone(
        self, backend_factory: MigratableBackendFactory
    ):
        backend = backend_factory("replace-drop")
        kept, _dropped = await self._seeded(backend, "kept", "dropped")

        await backend.session_migration.run(
            SESSION, RecordingMigrator(executions=(kept,))
        )

        assert [e.id for e in await _executions(backend)] == [kept.id]

    async def test_a_domain_the_result_dropped_leaves_no_version_behind(
        self, backend_factory: MigratableBackendFactory
    ):
        # A migration replaces the session outright, so a version it did not
        # carry over must not survive as a row an upsert would have left.
        backend = backend_factory("replace-versions")
        await self._seeded(backend, "task")
        await _claim_domain(backend, SESSION, DROPPED_DOMAIN, "v1")

        await backend.session_migration.run(
            SESSION, RecordingMigrator(domain_versions={DOMAIN: "v2"})
        )

        assert await _claim_domain(
            backend, SESSION, DROPPED_DOMAIN, "v-later"
        ) == DomainVersion("v-later")

    async def test_a_migrated_execution_keeps_its_result_and_metadata(
        self, backend_factory: MigratableBackendFactory
    ):
        backend = backend_factory("replace-payload")
        await _claim_domain(backend, SESSION, DOMAIN, "v1")
        execution = Execution.start(make_execution_id("task"), canonical_arguments())
        execution.complete(serialized_value("result"))
        execution.set_metadata("trace", serialized_value("trace"))
        await save_execution(backend, execution, SESSION)

        await backend.session_migration.run(SESSION, RecordingMigrator())

        (loaded,) = await _executions(backend)
        assert loaded == execution

    async def test_other_sessions_are_untouched(
        self, backend_factory: MigratableBackendFactory
    ):
        backend = backend_factory("replace-scope")
        await self._seeded(backend, "mine")
        await _claim_domain(backend, OTHER_SESSION, DOMAIN, "v1")
        theirs = Execution.start(make_execution_id("theirs"), canonical_arguments())
        await save_execution(backend, theirs, OTHER_SESSION)

        await backend.session_migration.run(
            SESSION, RecordingMigrator(domain_versions={DOMAIN: "v2"}, executions=())
        )

        assert [e.id for e in await _executions(backend, OTHER_SESSION)] == [theirs.id]
        assert await _claim_domain(
            backend, OTHER_SESSION, DOMAIN, "v-later"
        ) == DomainVersion("v1")

    async def test_the_result_survives_reopening_the_store(
        self, backend_factory: MigratableBackendFactory
    ):
        store = "replace-durable"
        backend = backend_factory(store)
        (seeded,) = await self._seeded(backend, "task")
        migrated = Execution.start(make_execution_id("renamed"), canonical_arguments())

        await backend.session_migration.run(
            SESSION,
            RecordingMigrator(domain_versions={DOMAIN: "v2"}, executions=(migrated,)),
        )

        reopened = backend_factory(store)
        assert [e.id for e in await _executions(reopened)] == [migrated.id]
        assert await _claim_domain(
            reopened, SESSION, DOMAIN, "v-later"
        ) == DomainVersion("v2")
        assert await reopened.repository.get(SESSION, seeded.id) is None

    # -- Nothing by halves ---------------------------------------------------

    async def test_a_migrator_that_raises_changes_nothing(
        self, backend_factory: MigratableBackendFactory
    ):
        backend = backend_factory("failure-migrator")
        seeded = await self._seeded(backend, "first", "second")

        with pytest.raises(RuntimeError, match="no thanks"):
            await backend.session_migration.run(
                SESSION, RecordingMigrator(raises=RuntimeError("no thanks"))
            )

        assert [e.id for e in await _executions(backend)] == [e.id for e in seeded]
        assert await _claim_domain(
            backend, SESSION, DOMAIN, "v-later"
        ) == DomainVersion("v1")

    async def test_a_migrator_that_raises_changes_nothing_on_disk_either(
        self, backend_factory: MigratableBackendFactory
    ):
        store = "failure-durable"
        backend = backend_factory(store)
        seeded = await self._seeded(backend, "task")

        with pytest.raises(RuntimeError):
            await backend.session_migration.run(
                SESSION, RecordingMigrator(raises=RuntimeError("no thanks"))
            )

        reopened = backend_factory(store)
        assert [e.id for e in await _executions(reopened)] == [e.id for e in seeded]
        assert await _claim_domain(
            reopened, SESSION, DOMAIN, "v-later"
        ) == DomainVersion("v1")

    async def test_two_executions_on_one_id_are_refused_before_anything_is_written(
        self, backend_factory: MigratableBackendFactory
    ):
        backend = backend_factory("failure-collision")
        (seeded,) = await self._seeded(backend, "task")
        collided = Execution.start(make_execution_id("merged"), canonical_arguments())

        def merge(source: StoredSession) -> StoredSession:
            # Building the result is where the collision is caught, so the store
            # is never asked to keep whichever copy is written last.
            return StoredSession(
                metadata=SessionMetadata.from_strings({DOMAIN: "v2"}),
                executions=(collided, replace(collided)),
            )

        with pytest.raises(MigrationCollisionError):
            await backend.session_migration.run(
                SESSION, RecordingMigrator(transform=merge)
            )

        assert [e.id for e in await _executions(backend)] == [seeded.id]
        assert await _claim_domain(
            backend, SESSION, DOMAIN, "v-later"
        ) == DomainVersion("v1")

    # -- Exclusion -----------------------------------------------------------

    async def test_cancellation_is_reported_only_once_the_migration_has_settled(
        self, backend_factory: MigratableBackendFactory
    ):
        # Cancelling does not stop the worker the migration is running on, so
        # reporting the cancellation before it has finished would leave the
        # caller free to act while the store is still being replaced.
        backend = backend_factory("cancellation")
        await self._seeded(backend, "before")
        after = Execution.start(make_execution_id("after"), canonical_arguments())

        inside = threading.Event()
        release = threading.Event()

        def hold() -> None:
            inside.set()
            release.wait(5)

        migration = asyncio.create_task(
            backend.session_migration.run(
                SESSION,
                RecordingMigrator(
                    domain_versions={DOMAIN: "v2"},
                    executions=(after,),
                    before_return=hold,
                ),
            )
        )
        await asyncio.to_thread(inside.wait, 5)

        migration.cancel()
        await asyncio.sleep(0.05)
        assert not migration.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await migration

        assert [e.id for e in await _executions(backend)] == [after.id]
        assert await _claim_domain(
            backend, SESSION, DOMAIN, "v-later"
        ) == DomainVersion("v2")

    async def test_another_writer_cannot_land_while_a_migration_holds_the_session(
        self, backend_factory: MigratableBackendFactory
    ):
        # Independent handles, because that is the shape of the hazard: a
        # migration run against a store some other worker is still writing to.
        # Sharing one handle would prove only that its own lock serializes.
        migrating = backend_factory("exclusion")
        writing = backend_factory("exclusion")
        await self._seeded(migrating, "before")
        after = Execution.start(make_execution_id("after"), canonical_arguments())
        intruder = Execution.start(make_execution_id("intruder"), canonical_arguments())

        inside = threading.Event()
        release = threading.Event()

        def hold() -> None:
            inside.set()
            release.wait(5)

        async def interrupt() -> None:
            async with TransactionScope(writing.transaction_provider):
                await writing.repository.save(SESSION, intruder)

        migration = asyncio.create_task(
            migrating.session_migration.run(
                SESSION,
                RecordingMigrator(
                    domain_versions={DOMAIN: "v2"},
                    executions=(after,),
                    before_return=hold,
                ),
            )
        )
        await asyncio.to_thread(inside.wait, 5)
        writer = asyncio.create_task(interrupt())
        await asyncio.sleep(0.05)

        # The migration reads, decides and writes as one: a save that landed in
        # between would either be dropped by a result built without it, or
        # overwrite that result.
        assert not writer.done()

        release.set()
        await migration
        await writer

        assert {e.id for e in await _executions(migrating)} == {after.id, intruder.id}
