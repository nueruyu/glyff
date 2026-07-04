from __future__ import annotations

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


class BackendHandle(Protocol):
    executions: ExecutionRepository
    transactions: TransactionProvider


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


def value(raw: bytes = b"value") -> SerializedValue:
    return SerializedValue(raw)


async def save_execution(backend: BackendHandle, execution: Execution) -> None:
    async with TransactionScope(backend.transactions):
        await backend.executions.save(execution)


class ExecutionBackendContract:
    @pytest.fixture
    def backend_factory(self) -> BackendFactory:
        raise NotImplementedError

    async def test_backend_exposes_separate_repository_and_transactions(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("shape")
        assert backend.executions is not backend.transactions
        assert isinstance(backend.executions, ExecutionRepository)
        assert isinstance(backend.transactions, TransactionProvider)
        assert not hasattr(backend.executions, "serializer")

    async def test_get_missing_returns_none(self, backend_factory: BackendFactory):
        backend = backend_factory("missing")
        assert await backend.executions.get(eid("missing")) is None

    async def test_save_started_then_get(self, backend_factory: BackendFactory):
        backend = backend_factory("started")
        execution_id = eid("task")

        await save_execution(backend, Execution.start(execution_id))

        loaded = await backend.executions.get(execution_id)
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
        execution.complete(value(b"result-bytes"))

        await save_execution(backend, execution)

        loaded = await backend.executions.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.COMPLETED
        assert loaded.result == value(b"result-bytes")

    async def test_save_preserves_metadata_inside_execution(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("metadata")
        execution_id = eid("task")
        execution = Execution.start(execution_id)
        execution.set_metadata("trace", value(b"trace-bytes"))
        execution.set_metadata("other", value(b"other-bytes"))

        await save_execution(backend, execution)

        loaded = await backend.executions.get(execution_id)
        assert loaded is not None
        assert loaded.get_metadata("trace") == Metadata("trace", value(b"trace-bytes"))
        assert loaded.get_metadata("other") == Metadata("other", value(b"other-bytes"))

    async def test_complete_preserves_existing_metadata(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("complete-keeps-metadata")
        execution_id = eid("task")
        execution = Execution.start(execution_id)
        execution.set_metadata("trace", value(b"trace"))
        execution.complete(value(b"result"))

        await save_execution(backend, execution)

        loaded = await backend.executions.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.COMPLETED
        assert loaded.result == value(b"result")
        assert loaded.get_metadata("trace") == Metadata("trace", value(b"trace"))

    async def test_save_overwrites_existing_aggregate(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("overwrite")
        execution_id = eid("task")

        first = Execution.start(execution_id)
        first.set_metadata("old", value(b"old"))
        await save_execution(backend, first)

        second = Execution.start(execution_id)
        second.set_metadata("new", value(b"new"))
        second.complete(value(b"done"))
        await save_execution(backend, second)

        loaded = await backend.executions.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.COMPLETED
        assert loaded.result == value(b"done")
        assert loaded.get_metadata("old") is None
        assert loaded.get_metadata("new") == Metadata("new", value(b"new"))

    async def test_save_requires_active_transaction(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("save-no-tx")
        with pytest.raises(RuntimeError):
            await backend.executions.save(Execution.start(eid("task")))

    async def test_delete_many_requires_active_transaction(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete-no-tx")
        with pytest.raises(RuntimeError):
            await backend.executions.delete_many([eid("task")])

    async def test_rollback_discards_save(self, backend_factory: BackendFactory):
        backend = backend_factory("rollback-save")
        execution_id = eid("task")

        tx = await backend.transactions.begin_transaction()
        await backend.executions.save(Execution.start(execution_id))
        await tx.rollback()

        assert await backend.executions.get(execution_id) is None

    async def test_commit_persists_save(self, backend_factory: BackendFactory):
        backend = backend_factory("commit-save")
        execution_id = eid("task")

        tx = await backend.transactions.begin_transaction()
        await backend.executions.save(Execution.start(execution_id))
        await tx.commit()

        assert await backend.executions.get(execution_id) is not None

    async def test_delete_many_removes_execution_and_metadata(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete")
        execution_id = eid("task")
        execution = Execution.start(execution_id)
        execution.set_metadata("trace", value(b"trace"))

        await save_execution(backend, execution)

        async with TransactionScope(backend.transactions):
            await backend.executions.delete_many([execution_id])

        assert await backend.executions.get(execution_id) is None

    async def test_delete_many_ignores_missing_ids(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete-missing")
        async with TransactionScope(backend.transactions):
            await backend.executions.delete_many([eid("missing")])

    async def test_delete_rollback_preserves_execution(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("delete-rollback")
        execution_id = eid("task")

        await save_execution(backend, Execution.start(execution_id))

        tx = await backend.transactions.begin_transaction()
        await backend.executions.delete_many([execution_id])
        await tx.rollback()

        loaded = await backend.executions.get(execution_id)
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

        async with TransactionScope(backend.transactions):
            for execution_id in [root, child, grandchild, sibling]:
                await backend.executions.save(Execution.start(execution_id))

        descendants = await backend.executions.descendants_of(root)
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
        first.complete(value(b"one"))
        second = Execution.start(leaf2)
        second.complete(value(b"two"))
        async with TransactionScope(backend.transactions):
            await backend.executions.save(first)
            await backend.executions.save(second)

        loaded1 = await backend.executions.get(leaf1)
        loaded2 = await backend.executions.get(leaf2)
        assert loaded1 is not None and loaded1.result == value(b"one")
        assert loaded2 is not None and loaded2.result == value(b"two")

    async def test_child_commit_survives_parent_rollback(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("nested-child-commit")
        root = eid("root")
        child = eid("child", parent=root)

        parent_tx = await backend.transactions.begin_transaction()
        await backend.executions.save(Execution.start(root))

        child_tx = await backend.transactions.begin_transaction()
        child_execution = Execution.start(child)
        child_execution.complete(value(b"child"))
        await backend.executions.save(child_execution)
        await child_tx.commit()

        await parent_tx.rollback()

        assert await backend.executions.get(root) is None
        loaded_child = await backend.executions.get(child)
        assert loaded_child is not None
        assert loaded_child.status is ExecutionStatus.COMPLETED

    async def test_child_rollback_does_not_affect_parent_staging(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("nested-child-rollback")
        root = eid("root")
        child = eid("child", parent=root)

        parent_tx = await backend.transactions.begin_transaction()
        await backend.executions.save(Execution.start(root))

        child_tx = await backend.transactions.begin_transaction()
        await backend.executions.save(Execution.start(child))
        await child_tx.rollback()

        staged_root = await backend.executions.get(root)
        assert staged_root is not None
        assert staged_root.status is ExecutionStatus.STARTED
        assert await backend.executions.get(child) is None

        await parent_tx.commit()

        committed_root = await backend.executions.get(root)
        assert committed_root is not None
        assert committed_root.status is ExecutionStatus.STARTED
        assert await backend.executions.get(child) is None

    async def test_out_of_order_transaction_close_raises(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("out-of-order")

        parent_tx = await backend.transactions.begin_transaction()
        child_tx = await backend.transactions.begin_transaction()

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

        root_tx = await backend.transactions.begin_transaction()
        await backend.executions.save(Execution.start(root))

        child_tx = await backend.transactions.begin_transaction()
        child_execution = Execution.start(child)

        grandchild_tx = await backend.transactions.begin_transaction()
        grandchild_execution = Execution.start(grandchild)
        grandchild_execution.complete(value(b"grandchild"))
        await backend.executions.save(grandchild_execution)
        await grandchild_tx.commit()

        child_execution.complete(value(b"child"))
        await backend.executions.save(child_execution)
        await child_tx.commit()

        await root_tx.rollback()

        assert await backend.executions.get(root) is None

        loaded_child = await backend.executions.get(child)
        assert loaded_child is not None
        assert loaded_child.status is ExecutionStatus.COMPLETED

        loaded_grandchild = await backend.executions.get(grandchild)
        assert loaded_grandchild is not None
        assert loaded_grandchild.status is ExecutionStatus.COMPLETED

    async def test_binary_serialized_value_roundtrips(
        self, backend_factory: BackendFactory
    ):
        backend = backend_factory("binary")
        execution_id = eid("task")
        binary = b"\xff\xfe\x00binary\x80"

        execution = Execution.start(execution_id)
        execution.complete(value(binary))
        execution.set_metadata("bin", value(binary))

        await save_execution(backend, execution)

        loaded = await backend.executions.get(execution_id)
        assert loaded is not None
        assert loaded.result == value(binary)
        assert loaded.get_metadata("bin") == Metadata("bin", value(binary))


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
        loaded = await reopened.executions.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.STARTED

    async def test_committed_delete_survives_reopen(
        self, backend_factory: BackendFactory
    ):
        session_id = "durable-delete"
        execution_id = eid("task")
        backend = backend_factory(session_id)

        await save_execution(backend, Execution.start(execution_id))

        async with TransactionScope(backend.transactions):
            await backend.executions.delete_many([execution_id])

        reopened = backend_factory(session_id)
        assert await reopened.executions.get(execution_id) is None

    async def test_rolled_back_delete_does_not_survive_reopen(
        self, backend_factory: BackendFactory
    ):
        session_id = "durable-delete-rollback"
        execution_id = eid("task")
        backend = backend_factory(session_id)

        await save_execution(backend, Execution.start(execution_id))

        tx = await backend.transactions.begin_transaction()
        await backend.executions.delete_many([execution_id])
        await tx.rollback()

        reopened = backend_factory(session_id)
        loaded = await reopened.executions.get(execution_id)
        assert loaded is not None
        assert loaded.status is ExecutionStatus.STARTED

    async def test_metadata_survives_reopen(self, backend_factory: BackendFactory):
        session_id = "durable-metadata"
        execution_id = eid("task")
        backend = backend_factory(session_id)
        execution = Execution.start(execution_id)
        execution.set_metadata("trace", value(b"trace"))

        await save_execution(backend, execution)

        reopened = backend_factory(session_id)
        loaded = await reopened.executions.get(execution_id)
        assert loaded is not None
        assert loaded.get_metadata("trace") == Metadata("trace", value(b"trace"))

    async def test_binary_serialized_value_survives_reopen(
        self, backend_factory: BackendFactory
    ):
        session_id = "durable-binary"
        execution_id = eid("task")
        binary = b"\xff\xfe\x00binary\x80"
        backend = backend_factory(session_id)

        execution = Execution.start(execution_id)
        execution.complete(value(binary))
        execution.set_metadata("bin", value(binary))

        await save_execution(backend, execution)

        reopened = backend_factory(session_id)
        loaded = await reopened.executions.get(execution_id)
        assert loaded is not None
        assert loaded.result == value(binary)
        assert loaded.get_metadata("bin") == Metadata("bin", value(binary))
