import hashlib

import pytest

from glyff import (
    ArgumentsDigest,
    CanonicalArguments,
    Execution,
    ExecutionStatus,
    SerializedValue,
)
from glyff.exceptions import ArgumentCanonicalizationError, InvalidExecutionError
from glyff.testing import canonical_arguments, make_execution_id


def test_canonical_arguments_encode_sorted_and_compact():
    assert CanonicalArguments.from_canonical({"b": 1, "a": [1, 2]}).data == (
        b'{"a":[1,2],"b":1}'
    )


def test_canonical_arguments_reject_values_outside_the_json_data_model():
    with pytest.raises(
        ArgumentCanonicalizationError, match="not in the JSON data model"
    ):
        CanonicalArguments.from_canonical({"a": {1, 2}})  # type: ignore[dict-item]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_arguments_reject_non_finite_numbers(value):
    with pytest.raises(ArgumentCanonicalizationError, match="JSON data model"):
        CanonicalArguments.from_canonical({"a": value})


@pytest.mark.parametrize("data", [b"not-json", b"[]", b'{"a":NaN}'])
def test_recorded_arguments_reject_malformed_canonical_data(data):
    with pytest.raises(InvalidExecutionError):
        CanonicalArguments(data).recorded()


def test_canonical_arguments_digest_is_sha256_of_their_bytes():
    arguments = CanonicalArguments.from_canonical({"a": 1})
    assert arguments.digest == ArgumentsDigest(
        hashlib.sha256(arguments.data).hexdigest()
    )


def test_start_creates_started_execution():
    execution = Execution.start(make_execution_id("task"), canonical_arguments())
    assert execution.status is ExecutionStatus.STARTED
    assert execution.result is None
    assert execution.metadata == {}


def test_complete_marks_execution_completed():
    execution = Execution.start(make_execution_id("task"), canonical_arguments())
    execution.complete(SerializedValue(b"1"))
    assert execution.status is ExecutionStatus.COMPLETED
    assert execution.result == SerializedValue(b"1")


def test_complete_terminal_execution_raises():
    execution = Execution.start(make_execution_id("task"), canonical_arguments())
    execution.complete(SerializedValue(b"1"))
    with pytest.raises(ValueError):
        execution.complete(SerializedValue(b"2"))


def test_set_metadata_keeps_metadata_inside_execution():
    execution = Execution.start(make_execution_id("task"), canonical_arguments())
    execution.set_metadata("trace_id", SerializedValue(b'"abc"'))
    metadata = execution.get_metadata("trace_id")
    assert metadata is not None
    assert metadata.key == "trace_id"
    assert metadata.value == SerializedValue(b'"abc"')


def test_execution_rejects_arguments_that_do_not_match_its_key():
    with pytest.raises(InvalidExecutionError, match="arguments_digest"):
        Execution.start(
            make_execution_id("task", arguments={"a": 1}), canonical_arguments({"b": 2})
        )


def test_completed_execution_must_carry_a_result():
    with pytest.raises(InvalidExecutionError, match="no result"):
        Execution(
            id=make_execution_id("task"),
            status=ExecutionStatus.COMPLETED,
            arguments=canonical_arguments(),
        )


def test_uncompleted_execution_must_not_carry_a_result():
    with pytest.raises(InvalidExecutionError, match="not completed"):
        Execution(
            id=make_execution_id("task"),
            status=ExecutionStatus.STARTED,
            arguments=canonical_arguments(),
            result=SerializedValue(b"1"),
        )
