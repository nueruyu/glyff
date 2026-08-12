"""The transaction-local stages every backend shares."""

import contextvars
from typing import cast

import pytest
from glyff import (
    CanonicalArguments,
    Execution,
    ExecutionStatus,
    SerializedValue,
    SessionId,
)
from glyff.store import staging as staging_module
from glyff.store.staging import (
    DeleteExecution,
    ExecutionKey,
    ExecutionStaging,
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
    assert staging_module.__all__ == [
        "DeleteExecution",
        "ExecutionKey",
        "ExecutionMutation",
        "ExecutionSnapshot",
        "ExecutionStage",
        "ExecutionStaging",
        "SaveExecution",
    ]
    assert all(hasattr(staging_module, name) for name in staging_module.__all__)


# -- Finding the open stage --------------------------------------------------


def test_there_is_no_open_stage_before_one_begins():
    assert ExecutionStaging().current() is None


def test_a_write_outside_a_stage_raises():
    staging = ExecutionStaging()

    with pytest.raises(RuntimeError, match="outside a transaction"):
        staging.require_current()


def test_the_stage_that_began_is_the_open_one():
    staging = ExecutionStaging()

    stage = staging.begin()

    assert staging.current() is stage
    assert staging.require_current() is stage


def test_a_nested_stage_hides_then_restores_its_parent():
    staging = ExecutionStaging()
    parent = staging.begin()

    child = staging.begin()
    assert staging.current() is child
    child.close()

    assert staging.current() is parent


# -- What a stage holds ------------------------------------------------------


def test_a_saved_execution_is_visible_to_lookup():
    stage = ExecutionStaging().begin()
    execution = started()

    stage.save(SESSION, execution)

    mutation = stage.lookup(SESSION, execution.id)
    assert isinstance(mutation, SaveExecution)
    assert mutation.snapshot.to_execution() == execution


def test_lookup_is_scoped_by_session():
    stage = ExecutionStaging().begin()
    execution = started()

    stage.save(SESSION, execution)

    assert stage.lookup(OTHER, execution.id) is None


def test_a_delete_masks_an_earlier_save():
    stage = ExecutionStaging().begin()
    execution = started()

    stage.save(SESSION, execution)
    stage.delete(SESSION, execution.id)

    assert isinstance(stage.lookup(SESSION, execution.id), DeleteExecution)


def test_a_later_save_replaces_a_delete():
    stage = ExecutionStaging().begin()
    execution = started()

    stage.delete(SESSION, execution.id)
    stage.save(SESSION, execution)

    assert isinstance(stage.lookup(SESSION, execution.id), SaveExecution)


def test_a_save_snapshots_the_execution():
    # The aggregate is mutable, so staging has to copy rather than reference it.
    stage = ExecutionStaging().begin()
    execution = started()

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
    stage = ExecutionStaging().begin()
    arguments = bytearray(canonical_arguments().data)
    result = bytearray(b'"result"')
    metadata = bytearray(b'"metadata"')

    # The casts are the point: this is the call a type checker cannot stop.
    execution = Execution.start(
        make_execution_id("task"),
        CanonicalArguments._from_recorded_bytes(cast(bytes, arguments)),
    )
    execution.complete(SerializedValue(cast(bytes, result)))
    execution.set_metadata("note", SerializedValue(cast(bytes, metadata)))

    stage.save(SESSION, execution)
    for payload in (arguments, result, metadata):
        payload[:] = b"!" * len(payload)

    mutation = stage.lookup(SESSION, execution.id)
    assert isinstance(mutation, SaveExecution)
    staged = mutation.snapshot.to_execution()
    assert staged.arguments.data == canonical_arguments().data
    assert staged.result == SerializedValue(b'"result"')
    assert staged.metadata["note"].value == SerializedValue(b'"metadata"')


def test_a_snapshot_is_detached_from_later_changes():
    stage = ExecutionStaging().begin()
    first = started("first")
    second = started("second")

    stage.save(SESSION, first)
    taken = stage.snapshot()
    stage.save(SESSION, second)

    assert set(taken) == {ExecutionKey(SESSION, first.id)}


# -- Closing -----------------------------------------------------------------


def test_the_batch_is_unavailable_while_the_stage_is_open():
    stage = ExecutionStaging().begin()
    stage.save(SESSION, started())

    with pytest.raises(RuntimeError, match="still open"):
        stage.batch


def test_closing_finalizes_the_batch():
    stage = ExecutionStaging().begin()
    execution = started()
    stage.save(SESSION, execution)

    stage.close()

    assert set(stage.batch) == {ExecutionKey(SESSION, execution.id)}


def test_the_batch_is_read_only_and_stable_after_close():
    stage = ExecutionStaging().begin()
    execution = started()
    stage.save(SESSION, execution)
    stage.close()

    batch = stage.batch
    with pytest.raises(TypeError):
        batch[ExecutionKey(OTHER, execution.id)] = DeleteExecution()  # type: ignore[index]

    assert stage.batch == batch


def test_a_closed_stage_refuses_further_writes():
    staging = ExecutionStaging()
    stage = staging.begin()
    stage.close()

    with pytest.raises(RuntimeError, match="closed"):
        stage.save(SESSION, started())
    with pytest.raises(RuntimeError, match="closed"):
        stage.delete(SESSION, started().id)


def test_a_closed_stage_refuses_writes_from_a_copied_context():
    # A context copied while the stage was open still holds it, so refusing has
    # to be the stage's own state rather than what happens to be current.
    staging = ExecutionStaging()
    stage = staging.begin()
    context = contextvars.copy_context()
    stage.close()

    with pytest.raises(RuntimeError, match="closed"):
        context.run(stage.save, SESSION, started())


def test_a_closed_stage_is_not_open_in_a_copied_context():
    # The copy still holds the stage and has no token to restore, so the only
    # safe answer is that nothing is open — otherwise a read there would overlay
    # a batch that may never have been persisted.
    staging = ExecutionStaging()
    stage = staging.begin()
    stage.save(SESSION, started())
    context = contextvars.copy_context()

    assert context.run(staging.current) is stage
    stage.close()

    assert context.run(staging.current) is None
    with pytest.raises(RuntimeError, match="outside a transaction"):
        context.run(staging.require_current)


def test_closing_twice_across_a_copied_context_raises():
    staging = ExecutionStaging()
    stage = staging.begin()
    context = contextvars.copy_context()
    stage.close()

    with pytest.raises(RuntimeError, match="out of order"):
        context.run(stage.close)


def test_closing_a_parent_while_a_child_is_open_raises():
    staging = ExecutionStaging()
    parent = staging.begin()
    staging.begin()

    with pytest.raises(RuntimeError, match="out of order"):
        parent.close()


def test_a_refused_close_leaves_the_stage_open():
    staging = ExecutionStaging()
    parent = staging.begin()
    child = staging.begin()
    execution = started()

    with pytest.raises(RuntimeError, match="out of order"):
        parent.close()

    # Not half-finalized: still writable, still without a batch.
    parent.save(SESSION, execution)
    with pytest.raises(RuntimeError, match="still open"):
        parent.batch

    child.close()
    parent.close()
    assert set(parent.batch) == {ExecutionKey(SESSION, execution.id)}


def test_closing_twice_raises():
    stage = ExecutionStaging().begin()
    stage.close()

    with pytest.raises(RuntimeError, match="out of order"):
        stage.close()
