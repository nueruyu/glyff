"""The transaction-local stage every backend shares."""

from typing import cast

import pytest
from glyff import (
    CanonicalArguments,
    Execution,
    ExecutionStatus,
    SerializedValue,
    SessionId,
)
from glyff.store import staging
from glyff.store.staging import (
    DeleteExecution,
    ExecutionKey,
    ExecutionStage,
    SaveExecution,
)
from glyff.testing import canonical_arguments, make_execution_id

SESSION = SessionId("test")
OTHER = SessionId("other")


def started(name: str = "task") -> Execution:
    return Execution.start(make_execution_id(name), canonical_arguments())


# -- The supported surface ---------------------------------------------------


def test_the_staging_types_are_importable_from_the_public_module():
    # Out-of-tree backends import these, so the module path and the names in it
    # are the promise; losing one has to be a deliberate edit here.
    assert staging.__all__ == [
        "DeleteExecution",
        "ExecutionKey",
        "ExecutionMutation",
        "ExecutionSnapshot",
        "ExecutionStage",
        "SaveExecution",
        "StageHandle",
    ]
    assert all(hasattr(staging, name) for name in staging.__all__)


# -- Staging -----------------------------------------------------------------


def test_a_write_outside_a_stage_raises():
    stage = ExecutionStage()

    with pytest.raises(RuntimeError, match="outside a transaction"):
        stage.save(SESSION, started())


def test_a_saved_execution_is_visible_to_lookup():
    stage = ExecutionStage()
    execution = started()

    stage.begin()
    stage.save(SESSION, execution)

    mutation = stage.lookup(SESSION, execution.id)
    assert isinstance(mutation, SaveExecution)
    assert mutation.snapshot.to_execution() == execution


def test_lookup_is_scoped_by_session():
    stage = ExecutionStage()
    execution = started()

    stage.begin()
    stage.save(SESSION, execution)

    assert stage.lookup(OTHER, execution.id) is None


def test_a_delete_masks_an_earlier_save():
    stage = ExecutionStage()
    execution = started()

    stage.begin()
    stage.save(SESSION, execution)
    stage.delete(SESSION, execution.id)

    assert isinstance(stage.lookup(SESSION, execution.id), DeleteExecution)


def test_a_later_save_replaces_a_delete():
    stage = ExecutionStage()
    execution = started()

    stage.begin()
    stage.delete(SESSION, execution.id)
    stage.save(SESSION, execution)

    assert isinstance(stage.lookup(SESSION, execution.id), SaveExecution)


def test_a_save_snapshots_the_execution():
    # The aggregate is mutable, so staging has to copy rather than reference it.
    stage = ExecutionStage()
    execution = started()

    stage.begin()
    stage.save(SESSION, execution)
    execution.complete(SerializedValue(b'"after"'))

    mutation = stage.lookup(SESSION, execution.id)
    assert isinstance(mutation, SaveExecution)
    staged = mutation.snapshot.to_execution()
    assert staged.status is ExecutionStatus.STARTED
    assert staged.result is None


def test_a_save_copies_the_payloads_it_snapshots():
    # The annotations say ``bytes``, but a mutable buffer satisfies them at
    # runtime, so the snapshot copies rather than trusting the caller.
    stage = ExecutionStage()
    arguments = bytearray(canonical_arguments().data)
    result = bytearray(b'"result"')
    metadata = bytearray(b'"metadata"')

    # The casts are the point: this is the call a type checker cannot stop.
    execution = Execution.start(
        make_execution_id("task"), CanonicalArguments(cast(bytes, arguments))
    )
    execution.complete(SerializedValue(cast(bytes, result)))
    execution.set_metadata("note", SerializedValue(cast(bytes, metadata)))

    stage.begin()
    stage.save(SESSION, execution)
    for payload in (arguments, result, metadata):
        payload[:] = b"!" * len(payload)

    mutation = stage.lookup(SESSION, execution.id)
    assert isinstance(mutation, SaveExecution)
    staged = mutation.snapshot.to_execution()
    assert staged.arguments.data == canonical_arguments().data
    assert staged.result == SerializedValue(b'"result"')
    assert staged.metadata["note"].value == SerializedValue(b'"metadata"')


def test_a_current_snapshot_is_detached_from_later_changes():
    stage = ExecutionStage()
    first = started("first")
    second = started("second")

    stage.begin()
    stage.save(SESSION, first)
    taken = stage.current_snapshot()
    stage.save(SESSION, second)

    assert set(taken) == {ExecutionKey(SESSION, first.id)}


def test_a_nested_stage_hides_then_restores_its_parent():
    stage = ExecutionStage()
    outer = started("outer")
    inner = started("inner")

    stage.begin()
    stage.save(SESSION, outer)

    child = stage.begin()
    stage.save(SESSION, inner)
    assert stage.lookup(SESSION, outer.id) is None
    stage.close(child)

    assert stage.lookup(SESSION, outer.id) is not None
    assert stage.lookup(SESSION, inner.id) is None


def test_sealing_returns_the_batch_and_refuses_further_writes():
    stage = ExecutionStage()
    execution = started()

    handle = stage.begin()
    stage.save(SESSION, execution)
    batch = stage.seal(handle)

    assert set(batch) == {ExecutionKey(SESSION, execution.id)}
    with pytest.raises(RuntimeError, match="closing"):
        stage.save(SESSION, execution)


def test_sealing_twice_raises():
    stage = ExecutionStage()
    handle = stage.begin()
    stage.seal(handle)

    with pytest.raises(RuntimeError, match="already sealed"):
        stage.seal(handle)


def test_closing_a_parent_while_a_child_is_current_raises():
    stage = ExecutionStage()
    parent = stage.begin()
    stage.begin()

    with pytest.raises(RuntimeError, match="out of order"):
        stage.close(parent)


def test_closing_twice_raises():
    stage = ExecutionStage()
    handle = stage.begin()
    stage.close(handle)

    with pytest.raises(RuntimeError, match="out of order"):
        stage.close(handle)
