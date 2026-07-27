"""pytest conformance contracts for glyff execution backends.

Re-exported from :mod:`glyff.testing`, the public entry point.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

import pytest

from glyff import (
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


class BackendHandle(Protocol):
    repository: ExecutionRepository
    transaction_provider: TransactionProvider


BackendFactory = Callable[[str], BackendHandle]


def eid(
    name: str,
    *,
    parent: ExecutionId | None = None,
    sequence: int = 0,
    args_hash: str = "h",
) -> ExecutionId:
    return ExecutionId(
        parent_id=parent,
        name=name,
        sequence=sequence,
        args_hash=args_hash,
    )


def value(raw: object = "value") -> SerializedValue:
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

    async def test_get_missing_returns_none(self, backend_factory: BackendFactory):
        backend = backend_factory("missing")
        assert await backend.repository.get(eid("missing")) is None

    async def test_save_started_then_get(self, backend_factory: BackendFactory):
        backend = backend_factory("started")
        execution_id = eid("task")

        await save_execution(backend, Execution.start(execution_id))

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.STARTED
        assert loaded.result is None
        assert loaded.metadata == {}

    async def test_save_completed_result_then_get(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("completed")
        execution_id = eid("task")
        execution = Execution.start(execution_id)
        execution.complete(value("result-bytes"))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.COMPLETED
        assert loaded.result == value("result-bytes")

    async def test_completed_json_null_result_roundtrips(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("completed-null")
        execution_id = eid("task")
        execution = Execution.start(execution_id)
        execution.complete(value(None))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.COMPLETED
        assert loaded.result == value(None)

    async def test_save_preserves_metadata_inside_execution(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("metadata")
        execution_id = eid("task")
        execution = Execution.start(execution_id)
        execution.set_metadata("trace", value("trace-bytes"))
        execution.set_metadata("other", value("other-bytes"))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.get_metadata("trace") == Metadata("trace", value("trace-bytes"))
        assert loaded.get_metadata("other") == Metadata("other", value("other-bytes"))

    async def test_complete_preserves_existing_metadata(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("complete-keeps-metadata")
        execution_id = eid("task")
        execution = Execution.start(execution_id)
        execution.set_metadata("trace", value("trace"))
        execution.complete(value("result"))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.COMPLETED
        assert loaded.result == value("result")
        assert loaded.get_metadata("trace") == Metadata("trace", value("trace"))

    async def test_save_overwrites_existing_aggregate(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("overwrite")
        execution_id = eid("task")

        first = Execution.start(execution_id)
        first.set_metadata("old", value("old"))
        await save_execution(backend, first)

        second = Execution.start(execution_id)
        second.set_metadata("new", value("new"))
        second.complete(value("done"))
        await save_execution(backend, second)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.COMPLETED
        assert loaded.result == value("done")
        assert loaded.get_metadata("old") is None
        assert loaded.get_metadata("new") == Metadata("new", value("new"))

    async def test_save_requires_active_transaction(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("save-no-tx")
        with pytest.raises(RuntimeError):
            await backend.repository.save(Execution.start(eid("task")))

    async def test_delete_many_requires_active_transaction(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete-no-tx")
        with pytest.raises(RuntimeError):
            await backend.repository.delete_many([eid("task")])

    async def test_rollback_discards_save(self, backend_factory: BackendFactory):
        backend = backend_factory("rollback-save")
        execution_id = eid("task")

        tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(Execution.start(execution_id))
        await tx.rollback()

        assert await backend.repository.get(execution_id) is None

    async def test_commit_persists_save(self, backend_factory: BackendFactory):
        backend = backend_factory("commit-save")
        execution_id = eid("task")

        tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(Execution.start(execution_id))
        await tx.commit()

        assert await backend.repository.get(execution_id) is not None

    async def test_delete_many_removes_execution_and_metadata(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete")
        execution_id = eid("task")
        execution = Execution.start(execution_id)
        execution.set_metadata("trace", value("trace"))

        await save_execution(backend, execution)

        async with TransactionScope(backend.transaction_provider):
            await backend.repository.delete_many([execution_id])

        assert await backend.repository.get(execution_id) is None

    async def test_delete_many_ignores_missing_ids(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete-missing")
        async with TransactionScope(backend.transaction_provider):
            await backend.repository.delete_many([eid("missing")])

    async def test_delete_rollback_preserves_execution(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete-rollback")
        execution_id = eid("task")

        await save_execution(backend, Execution.start(execution_id))

        tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.delete_many([execution_id])
        await tx.rollback()

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.STARTED

    async def test_descendants_of_returns_strict_descendants_only(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("descendants")
        root = eid("root")
        child = eid("child", parent=root)
        grandchild = eid("grandchild", parent=child)
        sibling = eid("sibling")

        async with TransactionScope(backend.transaction_provider):
            for execution_id in [root, child, grandchild, sibling]:
                await backend.repository.save(Execution.start(execution_id))

        descendants = await backend.repository.descendants_of(root)
        assert set(descendants) == {child, grandchild}
        assert root not in descendants
        assert sibling not in descendants

    async def test_same_frame_under_different_parents_do_not_collide(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("collision")
        p1 = eid("p1")
        p2 = eid("p2")
        # Identical (name, sequence, args_hash) frame under different parents
        # must remain independent records (the full-path key scheme guarantees
        # this; a flat key scheme would collide).
        leaf1 = eid("leaf", parent=p1, args_hash="same")
        leaf2 = eid("leaf", parent=p2, args_hash="same")

        first = Execution.start(leaf1)
        first.complete(value("one"))
        second = Execution.start(leaf2)
        second.complete(value("two"))
        async with TransactionScope(backend.transaction_provider):
            await backend.repository.save(first)
            await backend.repository.save(second)

        loaded1 = await backend.repository.get(leaf1)
        loaded2 = await backend.repository.get(leaf2)
        assert loaded1 is not None and loaded1.result == value("one")
        assert loaded2 is not None and loaded2.result == value("two")

    async def test_child_commit_survives_parent_rollback(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("nested-child-commit")
        root = eid("root")
        child = eid("child", parent=root)

        parent_tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(Execution.start(root))

        child_tx = await backend.transaction_provider.begin_transaction()
        child_execution = Execution.start(child)
        child_execution.complete(value("child"))
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
        root = eid("root")
        child = eid("child", parent=root)

        parent_tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(Execution.start(root))

        child_tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(Execution.start(child))
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
        root = eid("root")
        child = eid("child", parent=root)
        grandchild = eid("grandchild", parent=child)

        root_tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.save(Execution.start(root))

        child_tx = await backend.transaction_provider.begin_transaction()
        child_execution = Execution.start(child)

        grandchild_tx = await backend.transaction_provider.begin_transaction()
        grandchild_execution = Execution.start(grandchild)
        grandchild_execution.complete(value("grandchild"))
        await backend.repository.save(grandchild_execution)
        await grandchild_tx.commit()

        child_execution.complete(value("child"))
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

    async def test_json_serialized_value_roundtrips(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("json")
        execution_id = eid("task")
        payload = {"answer": 42, "items": [1, 2, 3]}
        metadata = {"trace": {"step": 1}, "ok": True}

        execution = Execution.start(execution_id)
        execution.complete(value(payload))
        execution.set_metadata("json", value(metadata))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.result == value(payload)
        assert loaded.get_metadata("json") == Metadata("json", value(metadata))


class TextBackendContract:
    @pytest.fixture
    def backend_factory(self) -> BackendFactory:
        raise NotImplementedError

    async def test_rejects_non_json_result_bytes(self, backend_factory: BackendFactory):
        backend = backend_factory("invalid-result")
        execution = Execution.start(eid("task"))
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
        execution = Execution.start(eid("task"))
        execution.set_metadata("trace", SerializedValue(b"not-json"))

        with pytest.raises(SerializationError) as excinfo:
            await save_execution(backend, execution)

        message = str(excinfo.value)
        assert "Text execution backends require SerializedValue.data" in message
        assert "UTF-8 encoded JSON text" in message
        assert "metadata key 'trace'" in message


class BinarySafeBackendContract:
    @pytest.fixture
    def backend_factory(self) -> BackendFactory:
        raise NotImplementedError

    async def test_roundtrips_arbitrary_result_bytes(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("binary-result")
        execution_id = eid("task")
        execution = Execution.start(execution_id)
        execution.complete(SerializedValue(b"\xff"))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.result == SerializedValue(b"\xff")

    async def test_roundtrips_arbitrary_metadata_bytes(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("binary-metadata")
        execution_id = eid("task")
        execution = Execution.start(execution_id)
        execution.set_metadata("trace", SerializedValue(b"not-json"))

        await save_execution(backend, execution)

        loaded = await backend.repository.get(execution_id)
        assert loaded is not None
        assert loaded.get_metadata("trace") == Metadata(
            "trace", SerializedValue(b"not-json")
        )


class DurableBackendContract:
    @pytest.fixture
    def backend_factory(self) -> BackendFactory:
        raise NotImplementedError

    async def test_committed_save_survives_reopen(
        self, backend_factory: BackendFactory
    ):
        session_id = "durable-save"
        execution_id = eid("task")
        backend = backend_factory(session_id)

        await save_execution(backend, Execution.start(execution_id))

        reopened = backend_factory(session_id)
        loaded = await reopened.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.STARTED

    async def test_committed_delete_survives_reopen(
        self, backend_factory: BackendFactory
    ):
        session_id = "durable-delete"
        execution_id = eid("task")
        backend = backend_factory(session_id)

        await save_execution(backend, Execution.start(execution_id))

        async with TransactionScope(backend.transaction_provider):
            await backend.repository.delete_many([execution_id])

        reopened = backend_factory(session_id)
        assert await reopened.repository.get(execution_id) is None

    async def test_rolled_back_delete_does_not_survive_reopen(
        self, backend_factory: BackendFactory
    ):
        session_id = "durable-delete-rollback"
        execution_id = eid("task")
        backend = backend_factory(session_id)

        await save_execution(backend, Execution.start(execution_id))

        tx = await backend.transaction_provider.begin_transaction()
        await backend.repository.delete_many([execution_id])
        await tx.rollback()

        reopened = backend_factory(session_id)
        loaded = await reopened.repository.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.STARTED

    async def test_metadata_survives_reopen(self, backend_factory: BackendFactory):
        session_id = "durable-metadata"
        execution_id = eid("task")
        backend = backend_factory(session_id)
        execution = Execution.start(execution_id)
        execution.set_metadata("trace", value("trace"))

        await save_execution(backend, execution)

        reopened = backend_factory(session_id)
        loaded = await reopened.repository.get(execution_id)
        assert loaded is not None
        assert loaded.get_metadata("trace") == Metadata("trace", value("trace"))

    async def test_json_serialized_value_survives_reopen(
        self, backend_factory: BackendFactory
    ):
        session_id = "durable-json"
        execution_id = eid("task")
        payload = {"answer": 42, "items": [1, 2, 3]}
        metadata = {"trace": {"step": 1}, "ok": True}
        backend = backend_factory(session_id)

        execution = Execution.start(execution_id)
        execution.complete(value(payload))
        execution.set_metadata("json", value(metadata))

        await save_execution(backend, execution)

        reopened = backend_factory(session_id)
        loaded = await reopened.repository.get(execution_id)
        assert loaded is not None
        assert loaded.result == value(payload)
        assert loaded.get_metadata("json") == Metadata("json", value(metadata))
