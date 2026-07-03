import pytest

from glyff import Execution, ExecutionId, ExecutionStatus, SerializedValue


def eid(name: str = "task") -> ExecutionId:
    return ExecutionId(parent_id=None, name=name, sequence=0, args_hash="hash")


def test_start_creates_started_execution():
    execution = Execution.start(eid())
    assert execution.status is ExecutionStatus.STARTED
    assert execution.result is None
    assert execution.error is None
    assert execution.metadata == {}


def test_complete_marks_execution_completed():
    execution = Execution.start(eid())
    execution.complete(SerializedValue(b"1"))
    assert execution.status is ExecutionStatus.COMPLETED
    assert execution.result == SerializedValue(b"1")
    assert execution.error is None


def test_complete_terminal_execution_raises():
    execution = Execution.start(eid())
    execution.complete(SerializedValue(b"1"))
    with pytest.raises(ValueError):
        execution.complete(SerializedValue(b"2"))


def test_fail_marks_execution_failed():
    execution = Execution.start(eid())
    execution.fail("boom")
    assert execution.status is ExecutionStatus.FAILED
    assert execution.error == "boom"


def test_set_metadata_keeps_metadata_inside_execution():
    execution = Execution.start(eid())
    execution.set_metadata("trace_id", SerializedValue(b'"abc"'))
    metadata = execution.get_metadata("trace_id")
    assert metadata is not None
    assert metadata.key == "trace_id"
    assert metadata.value == SerializedValue(b'"abc"')
