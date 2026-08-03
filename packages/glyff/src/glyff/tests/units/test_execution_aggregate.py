import pytest

from glyff import Execution, ExecutionStatus, SerializedValue
from glyff.exceptions import InvalidExecutionError
from glyff.testing import encoded_args, eid


def test_start_creates_started_execution():
    execution = Execution.start(eid("task"), encoded_args())
    assert execution.status is ExecutionStatus.STARTED
    assert execution.result is None
    assert execution.metadata == {}


def test_complete_marks_execution_completed():
    execution = Execution.start(eid("task"), encoded_args())
    execution.complete(SerializedValue(b"1"))
    assert execution.status is ExecutionStatus.COMPLETED
    assert execution.result == SerializedValue(b"1")


def test_complete_terminal_execution_raises():
    execution = Execution.start(eid("task"), encoded_args())
    execution.complete(SerializedValue(b"1"))
    with pytest.raises(ValueError):
        execution.complete(SerializedValue(b"2"))


def test_set_metadata_keeps_metadata_inside_execution():
    execution = Execution.start(eid("task"), encoded_args())
    execution.set_metadata("trace_id", SerializedValue(b'"abc"'))
    metadata = execution.get_metadata("trace_id")
    assert metadata is not None
    assert metadata.key == "trace_id"
    assert metadata.value == SerializedValue(b'"abc"')


def test_execution_rejects_arguments_that_do_not_match_its_key():
    with pytest.raises(InvalidExecutionError, match="args_hash"):
        Execution.start(eid("task", args={"a": 1}), encoded_args({"b": 2}))


def test_completed_execution_must_carry_a_result():
    with pytest.raises(InvalidExecutionError, match="no result"):
        Execution(
            id=eid("task"),
            status=ExecutionStatus.COMPLETED,
            args=encoded_args(),
        )


def test_uncompleted_execution_must_not_carry_a_result():
    with pytest.raises(InvalidExecutionError, match="not completed"):
        Execution(
            id=eid("task"),
            status=ExecutionStatus.STARTED,
            args=encoded_args(),
            result=SerializedValue(b"1"),
        )
