import hashlib

import pytest

from glyff import ArgumentsDigest, CanonicalArguments
from glyff.exceptions import ArgumentCanonicalizationError, InvalidExecutionError


def test_canonical_arguments_encode_sorted_and_compact():
    assert CanonicalArguments({"b": 1, "a": [1, 2]}).data == (b'{"a":[1,2],"b":1}')


def test_canonical_arguments_reject_values_outside_the_json_data_model():
    with pytest.raises(
        ArgumentCanonicalizationError, match="not a canonical argument value"
    ):
        CanonicalArguments({"a": {1, 2}})  # type: ignore[dict-item]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_arguments_reject_non_finite_numbers(value):
    with pytest.raises(ArgumentCanonicalizationError, match="JSON data model"):
        CanonicalArguments({"a": value})


@pytest.mark.parametrize("data", [b"not-json", b"[]", b'{"a":NaN}'])
def test_recorded_arguments_reject_malformed_canonical_data(data):
    with pytest.raises(InvalidExecutionError):
        CanonicalArguments._from_recorded_bytes(data).recorded()


def test_canonical_arguments_digest_is_sha256_of_their_bytes():
    arguments = CanonicalArguments({"a": 1})
    assert arguments.digest == ArgumentsDigest(
        hashlib.sha256(arguments.data).hexdigest()
    )
