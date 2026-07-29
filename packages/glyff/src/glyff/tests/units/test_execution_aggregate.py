import pytest

from glyff import Execution, ExecutionStatus, SerializedValue
from glyff.testing import canonical_args, eid


def test_start_creates_started_execution():
    execution = Execution.start(eid("task"), canonical_args())
    assert execution.status is ExecutionStatus.STARTED
    assert execution.result is None
    assert execution.metadata == {}


def test_complete_marks_execution_completed():
    execution = Execution.start(eid("task"), canonical_args())
    execution.complete(SerializedValue(b"1"))
    assert execution.status is ExecutionStatus.COMPLETED
    assert execution.result == SerializedValue(b"1")


def test_complete_terminal_execution_raises():
    execution = Execution.start(eid("task"), canonical_args())
    execution.complete(SerializedValue(b"1"))
    with pytest.raises(ValueError):
        execution.complete(SerializedValue(b"2"))


def test_set_metadata_keeps_metadata_inside_execution():
    execution = Execution.start(eid("task"), canonical_args())
    execution.set_metadata("trace_id", SerializedValue(b'"abc"'))
    metadata = execution.get_metadata("trace_id")
    assert metadata is not None
    assert metadata.key == "trace_id"
    assert metadata.value == SerializedValue(b'"abc"')
