"""pytest conformance contracts for glyff execution backends.

Re-exported from :mod:`glyff.testing`, the public entry point.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Protocol

import pytest

from glyff import (
    AppVersionStore,
    CanonicalValue,
    CanonicalArguments,
    Execution,
    ExecutionId,
    ExecutionRepository,
    ExecutionStatus,
    Metadata,
    SerializedValue,
    TransactionProvider,
    TransactionScope,
)
from glyff.exceptions import SerializationError
from glyff.serialization._utils import encode_canonical


class BackendHandle(Protocol):
    repository: ExecutionRepository
    transaction_provider: TransactionProvider
    session_id: str | None
    app_version_store: AppVersionStore | None


BackendFactory = Callable[[str], BackendHandle]


def make_execution_id(
    name: str,
    *,
    parent: ExecutionId | None = None,
    sequence: int = 0,
    arguments: dict[str, CanonicalValue] | None = None,
) -> ExecutionId:
    """An execution id keyed by ``arguments``, which :func:`canonical_arguments` records."""
    return ExecutionId(
        parent_id=parent,
        name=name,
        sequence=sequence,
        arguments_digest=canonical_arguments(arguments).digest,
    )


def canonical_arguments(
    arguments: dict[str, CanonicalValue] | None = None,
) -> CanonicalArguments:
    """The bound arguments an id built by :func:`make_execution_id` is keyed by."""
    return CanonicalArguments(encode_canonical(arguments or {}))


def serialized_value(raw: object = "value") -> SerializedValue:
    return SerializedValue(
        json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


async def save_execution(backend: BackendHandle, execution: Execution) -> None:
    async with TransactionScope(backend.transaction_provider):
        await backend.repository.save(execution)


class ExecutionBackendContract:
    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def backend_factory(self) -> BackendFactory:
        raise NotImplementedError

    async def test_backend_exposes_separate_repository_and_transaction_provider(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("shape")
        assert backend.repository is not backend.transaction_provider
        assert isinstance(backend.repository, ExecutionRepository)
        assert isinstance(backend.transaction_provider, TransactionProvider)
        assert not hasattr(backend.repository, "serializer")
        # None is a valid answer: a store too ephemeral to outlive its code.
        assert backend.app_version_store is None or isinstance(
            backend.app_version_store, AppVersionStore
        )

    async def test_backend_names_the_session_its_records_belong_to(
        self, backend_factory: BackendFactory
    ):
        # A store the session cannot identify is one a mistyped session id can
        # silently write into.
        backend = backend_factory("named-session")
        assert backend.session_id in (None, "named-session")

    async def test_get_missing_returns_none(self, backend_factory: BackendFactory):
        backend = backend_factory("missing")
        assert await backend.repository.get(make_execution_id("missing")) is None

    async def test_save_started_then_get(self, backend_factory: BackendFactory):
        backend = backend_factory("started")
        execution_id = make_execution_id("task")

        await save_execution(
            backend, Execution.start(execution_id, canonical_arguments())
        )

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.STARTED
        assert loaded.result is None
        assert loaded.metadata == {}

    async def test_args_roundtrip_byte_for_byte(self, backend_factory: BackendFactory):
        backend = backend_factory("args-bytes")
        raw = {"q": "こんにちは", "n": 1}
        execution_id = make_execution_id("task", arguments=raw)
        args = canonical_arguments(raw)

        await save_execution(backend, Execution.start(execution_id, args))

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        # Byte equality, not JSON equality: non-ASCII catches a store that re-encodes.
        assert loaded.arguments.data == args.data

    async def test_completed_execution_keeps_its_args(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("args-completed")
        execution_id = make_execution_id("task", arguments={"a": 1})
        args = canonical_arguments({"a": 1})
        execution = Execution.start(execution_id, args)
        execution.complete(serialized_value("result"))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.arguments.data == args.data

    async def test_save_completed_result_then_get(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("completed")
        execution_id = make_execution_id("task")
        execution = Execution.start(execution_id, canonical_arguments())
        execution.complete(serialized_value("result-bytes"))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.COMPLETED
        assert loaded.result == serialized_value("result-bytes")

    async def test_completed_json_null_result_roundtrips(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("completed-null")
        execution_id = make_execution_id("task")
        execution = Execution.start(execution_id, canonical_arguments())
        execution.complete(serialized_value(None))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.COMPLETED
        assert loaded.result == serialized_value(None)

    async def test_save_preserves_metadata_inside_execution(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("metadata")
        execution_id = make_execution_id("task")
        execution = Execution.start(execution_id, canonical_arguments())
        execution.set_metadata("trace", serialized_value("trace-bytes"))
        execution.set_metadata("other", serialized_value("other-bytes"))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.get_metadata("trace") == Metadata(
            "trace", serialized_value("trace-bytes")
        )
        assert loaded.get_metadata("other") == Metadata(
            "other", serialized_value("other-bytes")
        )

    async def test_complete_preserves_existing_metadata(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("complete-keeps-metadata")
        execution_id = make_execution_id("task")
        execution = Execution.start(execution_id, canonical_arguments())
        execution.set_metadata("trace", serialized_value("trace"))
        execution.complete(serialized_value("result"))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.COMPLETED
        assert loaded.result == serialized_value("result")
        assert loaded.get_metadata("trace") == Metadata(
            "trace", serialized_value("trace")
        )

    async def test_save_overwrites_existing_aggregate(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("overwrite")
        execution_id = make_execution_id("task")

        first = Execution.start(execution_id, canonical_arguments())
        first.set_metadata("old", serialized_value("old"))
        await save_execution(backend, first)

        second = Execution.start(execution_id, canonical_arguments())
        second.set_metadata("new", serialized_value("new"))
        second.complete(serialized_value("done"))
        await save_execution(backend, second)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.COMPLETED
        assert loaded.result == serialized_value("done")
        assert loaded.get_metadata("old") is None
        assert loaded.get_metadata("new") == Metadata("new", serialized_value("new"))

    async def test_save_requires_active_transaction(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("save-no-tx")
        with pytest.raises(RuntimeError):
            await backend.repository.save(
                Execution.start(make_execution_id("task"), canonical_arguments())
            )

    async def test_delete_many_requires_active_transaction(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete-no-tx")
        with pytest.raises(RuntimeError):
            await backend.repository.delete_many([make_execution_id("task")])

    async def test_rollback_discards_save(self, backend_factory: BackendFactory):
        backend = backend_factory("rollback-save")
        execution_id = make_execution_id("task")

        tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(
            Execution.start(execution_id, canonical_arguments())
        )
        await tx.rollback()

        assert await backend.repository.get(execution_id) is None

    async def test_commit_persists_save(self, backend_factory: BackendFactory):
        backend = backend_factory("commit-save")
        execution_id = make_execution_id("task")

        tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(
            Execution.start(execution_id, canonical_arguments())
        )
        await tx.commit()

        assert await backend.repository.get(execution_id) is not None

    async def test_delete_many_removes_execution_and_metadata(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete")
        execution_id = make_execution_id("task")
        execution = Execution.start(execution_id, canonical_arguments())
        execution.set_metadata("trace", serialized_value("trace"))

        await save_execution(backend, execution)

        async with TransactionScope(backend.transaction_provider):
            await backend.repository.delete_many([execution_id])

        assert await backend.repository.get(execution_id) is None

    async def test_delete_many_ignores_missing_ids(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete-missing")
        async with TransactionScope(backend.transaction_provider):
            await backend.repository.delete_many([make_execution_id("missing")])

    async def test_delete_rollback_preserves_execution(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete-rollback")
        execution_id = make_execution_id("task")

        await save_execution(
            backend, Execution.start(execution_id, canonical_arguments())
        )

        tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.delete_many([execution_id])
        await tx.rollback()

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.STARTED

    async def test_executions_is_empty_for_a_fresh_store(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("enumerate-empty")
        assert [e async for e in backend.repository.executions()] == []

    async def test_executions_yields_each_record_after_its_ancestors(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("enumerate")
        root = make_execution_id("root")
        child = make_execution_id("child", parent=root)
        grandchild = make_execution_id("grandchild", parent=child)

        async with TransactionScope(backend.transaction_provider):
            # Saved leaf-first: the order comes from the contract, not insertion.
            for execution_id in [grandchild, child, root]:
                await backend.repository.save(
                    Execution.start(execution_id, canonical_arguments())
                )

        yielded = [e.id async for e in backend.repository.executions()]
        assert set(yielded) == {root, child, grandchild}
        assert yielded.index(root) < yielded.index(child) < yielded.index(grandchild)

    async def test_executions_yields_whole_aggregates(
        self, backend_factory: BackendFactory
    ):
        # Enumeration returns Executions rather than ids so a consumer that needs
        # arguments or results does not have to read every record back.
        backend = backend_factory("enumerate-aggregate")
        execution = Execution.start(
            make_execution_id("task", arguments={"a": 1}),
            canonical_arguments({"a": 1}),
        )
        execution.set_metadata("trace", serialized_value("trace"))
        execution.complete(serialized_value("done"))

        await save_execution(backend, execution)

        (loaded,) = [e async for e in backend.repository.executions()]
        assert loaded.arguments.data == canonical_arguments({"a": 1}).data
        assert loaded.result == serialized_value("done")
        assert loaded.get_metadata("trace") == Metadata(
            "trace", serialized_value("trace")
        )

    async def test_executions_under_returns_strict_descendants_only(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("enumerate-under")
        root = make_execution_id("root")
        child = make_execution_id("child", parent=root)
        grandchild = make_execution_id("grandchild", parent=child)
        sibling = make_execution_id("sibling")

        async with TransactionScope(backend.transaction_provider):
            for execution_id in [root, child, grandchild, sibling]:
                await backend.repository.save(
                    Execution.start(execution_id, canonical_arguments())
                )

        under_root = {e.id async for e in backend.repository.executions(under=root)}
        assert under_root == {child, grandchild}
        assert {
            e.id async for e in backend.repository.executions(under=grandchild)
        } == set()

    async def test_executions_filters_by_status(self, backend_factory: BackendFactory):
        backend = backend_factory("enumerate-status")
        started = make_execution_id("started")
        completed = Execution.start(
            make_execution_id("completed"), canonical_arguments()
        )
        completed.complete(serialized_value("done"))

        async with TransactionScope(backend.transaction_provider):
            await backend.repository.save(
                Execution.start(started, canonical_arguments())
            )
            await backend.repository.save(completed)

        repository = backend.repository
        assert [
            e.id async for e in repository.executions(status=ExecutionStatus.STARTED)
        ] == [started]
        assert [
            e.id async for e in repository.executions(status=ExecutionStatus.COMPLETED)
        ] == [completed.id]

    async def test_executions_sees_records_staged_in_the_open_transaction(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("enumerate-staged")
        execution_id = make_execution_id("task")

        tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(
            Execution.start(execution_id, canonical_arguments())
        )
        staged = [e.id async for e in backend.repository.executions()]
        await tx.rollback()

        assert staged == [execution_id]
        assert [e async for e in backend.repository.executions()] == []

    async def test_executions_omits_records_deleted_in_the_open_transaction(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("enumerate-staged-delete")
        execution_id = make_execution_id("task")
        await save_execution(
            backend, Execution.start(execution_id, canonical_arguments())
        )

        tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.delete_many([execution_id])
        staged = [e.id async for e in backend.repository.executions()]
        await tx.rollback()

        assert staged == []

    async def test_same_frame_under_different_parents_do_not_collide(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("collision")
        p1 = make_execution_id("p1")
        p2 = make_execution_id("p2")
        # Identical (name, sequence, arguments_digest) frame under different parents
        # must remain independent records (the full-path key scheme guarantees
        # this; a flat key scheme would collide).
        leaf1 = make_execution_id("leaf", parent=p1, arguments={"a": "same"})
        leaf2 = make_execution_id("leaf", parent=p2, arguments={"a": "same"})

        first = Execution.start(leaf1, canonical_arguments({"a": "same"}))
        first.complete(serialized_value("one"))
        second = Execution.start(leaf2, canonical_arguments({"a": "same"}))
        second.complete(serialized_value("two"))
        async with TransactionScope(backend.transaction_provider):
            await backend.repository.save(first)
            await backend.repository.save(second)

        loaded1 = await backend.repository.get(leaf1)
        loaded2 = await backend.repository.get(leaf2)
        assert loaded1 is not None and loaded1.result == serialized_value("one")
        assert loaded2 is not None and loaded2.result == serialized_value("two")

    async def test_child_commit_survives_parent_rollback(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("nested-child-commit")
        root = make_execution_id("root")
        child = make_execution_id("child", parent=root)

        parent_tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(Execution.start(root, canonical_arguments()))

        child_tx = await backend.transaction_provider.begin_transaction()
        child_execution = Execution.start(child, canonical_arguments())
        child_execution.complete(serialized_value("child"))
        await backend.repository.save(child_execution)
        await child_tx.commit()

        await parent_tx.rollback()

        assert await backend.repository.get(root) is None
        loaded_child = await backend.repository.get(child)
        assert loaded_child is not None
        assert loaded_child.status is ExecutionStatus.COMPLETED

    async def test_child_rollback_does_not_affect_parent_staging(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("nested-child-rollback")
        root = make_execution_id("root")
        child = make_execution_id("child", parent=root)

        parent_tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(Execution.start(root, canonical_arguments()))

        child_tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(Execution.start(child, canonical_arguments()))
        await child_tx.rollback()

        staged_root = await backend.repository.get(root)
        assert staged_root is not None
        assert staged_root.status is ExecutionStatus.STARTED
        assert await backend.repository.get(child) is None

        await parent_tx.commit()

        committed_root = await backend.repository.get(root)
        assert committed_root is not None
        assert committed_root.status is ExecutionStatus.STARTED
        assert await backend.repository.get(child) is None

    async def test_out_of_order_transaction_close_raises(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("out-of-order")

        parent_tx = await backend.transaction_provider.begin_transaction()
        child_tx = await backend.transaction_provider.begin_transaction()

        with pytest.raises(RuntimeError):
            await parent_tx.commit()

        await child_tx.rollback()
        await parent_tx.rollback()

    async def test_three_level_nested_transactions_restore_each_parent(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("three-level")
        root = make_execution_id("root")
        child = make_execution_id("child", parent=root)
        grandchild = make_execution_id("grandchild", parent=child)

        root_tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(Execution.start(root, canonical_arguments()))

        child_tx = await backend.transaction_provider.begin_transaction()
        child_execution = Execution.start(child, canonical_arguments())

        grandchild_tx = await backend.transaction_provider.begin_transaction()
        grandchild_execution = Execution.start(grandchild, canonical_arguments())
        grandchild_execution.complete(serialized_value("grandchild"))
        await backend.repository.save(grandchild_execution)
        await grandchild_tx.commit()

        child_execution.complete(serialized_value("child"))
        await backend.repository.save(child_execution)
        await child_tx.commit()

        await root_tx.rollback()

        assert await backend.repository.get(root) is None

        loaded_child = await backend.repository.get(child)
        assert loaded_child is not None
        assert loaded_child.status is ExecutionStatus.COMPLETED

        loaded_grandchild = await backend.repository.get(grandchild)
        assert loaded_grandchild is not None
        assert loaded_grandchild.status is ExecutionStatus.COMPLETED

    async def test_json_value_roundtrips(self, backend_factory: BackendFactory):
        backend = backend_factory("json")
        execution_id = make_execution_id("task")
        payload = {"answer": 42, "items": [1, 2, 3]}
        metadata = {"trace": {"step": 1}, "ok": True}

        execution = Execution.start(execution_id, canonical_arguments())
        execution.complete(serialized_value(payload))
        execution.set_metadata("json", serialized_value(metadata))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.result == serialized_value(payload)
        assert loaded.get_metadata("json") == Metadata(
            "json", serialized_value(metadata)
        )


class TextBackendContract:
    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def backend_factory(self) -> BackendFactory:
        raise NotImplementedError

    async def test_rejects_non_json_result_bytes(self, backend_factory: BackendFactory):
        backend = backend_factory("invalid-result")
        execution = Execution.start(make_execution_id("task"), canonical_arguments())
        execution.complete(SerializedValue(b"\xff"))

        with pytest.raises(SerializationError) as excinfo:
            await save_execution(backend, execution)

        message = str(excinfo.value)
        assert "Text execution backends require SerializedValue.data" in message
        assert "UTF-8 encoded JSON text" in message
        assert "result" in message

    async def test_rejects_non_json_metadata_bytes(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("invalid-metadata")
        execution = Execution.start(make_execution_id("task"), canonical_arguments())
        execution.set_metadata("trace", SerializedValue(b"not-json"))

        with pytest.raises(SerializationError) as excinfo:
            await save_execution(backend, execution)

        message = str(excinfo.value)
        assert "Text execution backends require SerializedValue.data" in message
        assert "UTF-8 encoded JSON text" in message
        assert "metadata key 'trace'" in message


class BinarySafeBackendContract:
    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def backend_factory(self) -> BackendFactory:
        raise NotImplementedError

    async def test_roundtrips_arbitrary_result_bytes(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("binary-result")
        execution_id = make_execution_id("task")
        execution = Execution.start(execution_id, canonical_arguments())
        execution.complete(SerializedValue(b"\xff"))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.result == SerializedValue(b"\xff")

    async def test_roundtrips_arbitrary_metadata_bytes(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("binary-metadata")
        execution_id = make_execution_id("task")
        execution = Execution.start(execution_id, canonical_arguments())
        execution.set_metadata("trace", SerializedValue(b"not-json"))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.get_metadata("trace") == Metadata(
            "trace", SerializedValue(b"not-json")
        )


class AppVersionContract:
    """For a backend that records the application version behind its records.

    A backend whose store cannot outlive the code that wrote it exposes
    ``app_version_store = None`` and does not run this contract.
    """

    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def backend_factory(self) -> BackendFactory:
        raise NotImplementedError

    @staticmethod
    def _versions(backend: BackendHandle) -> AppVersionStore:
        assert backend.app_version_store is not None
        return backend.app_version_store

    async def test_fresh_store_records_no_version(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("version-fresh")
        assert await self._versions(backend).read() is None

    async def test_written_version_is_readable(self, backend_factory: BackendFactory):
        backend = backend_factory("version-write")

        async with TransactionScope(backend.transaction_provider):
            await self._versions(backend).write("v1")

        assert await self._versions(backend).read() == "v1"

    async def test_claim_takes_an_unclaimed_store(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("version-claim")
        store = self._versions(backend)

        assert await store.claim("v1") == "v1"
        assert await store.read() == "v1"

    async def test_claim_yields_to_the_incumbent(self, backend_factory: BackendFactory):
        backend = backend_factory("version-claim-taken")
        store = self._versions(backend)
        await store.claim("v1")

        assert await store.claim("v2") == "v1"
        assert await store.read() == "v1"

    async def test_concurrent_claims_agree_on_one_winner(
        self, backend_factory: BackendFactory
    ):
        # Read-then-write would let both find the store unclaimed and both start,
        # mixing two generations of records under whichever committed last.
        backend = backend_factory("version-claim-race")
        store = self._versions(backend)

        outcomes = await asyncio.gather(*(store.claim(f"v{n}") for n in range(8)))

        recorded = await store.read()
        assert set(outcomes) == {recorded}

    async def test_claim_does_not_need_a_transaction(
        self, backend_factory: BackendFactory
    ):
        # It is its own transaction: the check and the write cannot be split.
        backend = backend_factory("version-claim-no-tx")
        assert await self._versions(backend).claim("v1") == "v1"

    async def test_write_requires_active_transaction(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("version-no-tx")
        with pytest.raises(RuntimeError):
            await self._versions(backend).write("v1")

    async def test_rollback_discards_the_version(self, backend_factory: BackendFactory):
        backend = backend_factory("version-rollback")

        tx = await backend.transaction_provider.begin_transaction()
        await self._versions(backend).write("v1")
        await tx.rollback()

        assert await self._versions(backend).read() is None

    async def test_later_write_replaces_the_version(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("version-replace")

        async with TransactionScope(backend.transaction_provider):
            await self._versions(backend).write("v1")
        async with TransactionScope(backend.transaction_provider):
            await self._versions(backend).write("v2")

        assert await self._versions(backend).read() == "v2"

    async def test_version_commits_with_the_records_beside_it(
        self, backend_factory: BackendFactory
    ):
        # What a migration needs: rewritten records and the version they were
        # rewritten to become durable together, so neither can be observed alone.
        backend = backend_factory("version-atomic")
        execution_id = make_execution_id("task")

        async with TransactionScope(backend.transaction_provider):
            await backend.repository.save(
                Execution.start(execution_id, canonical_arguments())
            )
            await self._versions(backend).write("v2")

        reopened = backend_factory("version-atomic")
        assert await self._versions(reopened).read() == "v2"
        assert await reopened.repository.get(execution_id) is not None

    async def test_version_survives_reopen(self, backend_factory: BackendFactory):
        session_id = "version-durable"
        backend = backend_factory(session_id)

        async with TransactionScope(backend.transaction_provider):
            await self._versions(backend).write("v1")

        reopened = backend_factory(session_id)
        assert await self._versions(reopened).read() == "v1"

    async def test_version_write_leaves_the_format_version_readable(
        self, backend_factory: BackendFactory
    ):
        # Both versions share one slot; writing the application's must not drop
        # glyff's, or reopening the store would fail its own format check.
        session_id = "version-coexist"
        backend = backend_factory(session_id)

        async with TransactionScope(backend.transaction_provider):
            await self._versions(backend).write("v1")

        backend_factory(session_id)


class DurableBackendContract:
    pytestmark = pytest.mark.asyncio

    @pytest.fixture
    def backend_factory(self) -> BackendFactory:
        raise NotImplementedError

    async def test_committed_save_survives_reopen(
        self, backend_factory: BackendFactory
    ):
        session_id = "durable-save"
        execution_id = make_execution_id("task")
        backend = backend_factory(session_id)

        await save_execution(
            backend, Execution.start(execution_id, canonical_arguments())
        )

        reopened = backend_factory(session_id)
        loaded = await reopened.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.STARTED

    async def test_committed_delete_survives_reopen(
        self, backend_factory: BackendFactory
    ):
        session_id = "durable-delete"
        execution_id = make_execution_id("task")
        backend = backend_factory(session_id)

        await save_execution(
            backend, Execution.start(execution_id, canonical_arguments())
        )

        async with TransactionScope(backend.transaction_provider):
            await backend.repository.delete_many([execution_id])

        reopened = backend_factory(session_id)
        assert await reopened.repository.get(execution_id) is None

    async def test_rolled_back_delete_does_not_survive_reopen(
        self, backend_factory: BackendFactory
    ):
        session_id = "durable-delete-rollback"
        execution_id = make_execution_id("task")
        backend = backend_factory(session_id)

        await save_execution(
            backend, Execution.start(execution_id, canonical_arguments())
        )

        tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.delete_many([execution_id])
        await tx.rollback()

        reopened = backend_factory(session_id)
        loaded = await reopened.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.STARTED

    async def test_metadata_survives_reopen(self, backend_factory: BackendFactory):
        session_id = "durable-metadata"
        execution_id = make_execution_id("task")
        backend = backend_factory(session_id)
        execution = Execution.start(execution_id, canonical_arguments())
        execution.set_metadata("trace", serialized_value("trace"))

        await save_execution(backend, execution)

        reopened = backend_factory(session_id)
        loaded = await reopened.repository.get(execution_id)
        assert loaded is not None
        assert loaded.get_metadata("trace") == Metadata(
            "trace", serialized_value("trace")
        )

    async def test_json_value_survives_reopen(self, backend_factory: BackendFactory):
        session_id = "durable-json"
        execution_id = make_execution_id("task")
        payload = {"answer": 42, "items": [1, 2, 3]}
        metadata = {"trace": {"step": 1}, "ok": True}
        backend = backend_factory(session_id)

        execution = Execution.start(execution_id, canonical_arguments())
        execution.complete(serialized_value(payload))
        execution.set_metadata("json", serialized_value(metadata))

        await save_execution(backend, execution)

        reopened = backend_factory(session_id)
        loaded = await reopened.repository.get(execution_id)
        assert loaded is not None
        assert loaded.result == serialized_value(payload)
        assert loaded.get_metadata("json") == Metadata(
            "json", serialized_value(metadata)
        )
