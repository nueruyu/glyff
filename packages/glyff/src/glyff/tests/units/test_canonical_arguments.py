import hashlib

import pytest

from glyff import ArgumentsDigest, CanonicalArguments, CanonicalFallback
from glyff.exceptions import ArgumentCanonicalizationError, InvalidExecutionError


def test_canonical_arguments_encode_sorted_and_compact():
    assert CanonicalArguments({"b": 1, "a": [1, 2]}).data == (b'{"a":[1,2],"b":1}')


def test_canonical_arguments_reject_values_outside_the_json_data_model():
    with pytest.raises(
        ArgumentCanonicalizationError, match="not a canonical argument value"
    ):
        CanonicalArguments({"a": {1, 2}})  # type: ignore[dict-item]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_arguments_reject_non_finite_numbers(value: float) -> None:
    with pytest.raises(ArgumentCanonicalizationError, match="JSON data model"):
        CanonicalArguments({"a": value})


@pytest.mark.parametrize("data", [b"not-json", b"[]", b'{"a":NaN}'])
def test_decode_rejects_malformed_canonical_data(data: bytes) -> None:
    with pytest.raises(InvalidExecutionError):
        CanonicalArguments.from_recorded_bytes(data)


def test_decode_restores_nested_fallbacks():
    arguments = CanonicalArguments(
        {"clients": [CanonicalFallback("com.example.Client")]}
    )

    assert arguments.decode() == {"clients": [CanonicalFallback("com.example.Client")]}


def test_recorded_bytes_must_use_the_canonical_encoding():
    with pytest.raises(InvalidExecutionError, match="canonical encoding"):
        CanonicalArguments.from_recorded_bytes(b'{"b": 2, "a": 1}')


def test_decode_recursively_restores_nested_fallbacks():
    arguments = CanonicalArguments(
        {"started_at": CanonicalFallback(CanonicalFallback("2024-01-01"))}
    )

    assert arguments.decode() == {
        "started_at": CanonicalFallback(CanonicalFallback("2024-01-01"))
    }


def test_recorded_mapping_cannot_claim_the_reserved_fallback_key():
    with pytest.raises(InvalidExecutionError, match="reserved fallback key"):
        CanonicalArguments.from_recorded_bytes(
            b'{"value":{"__glyff_fallback__":"x","other":1}}'
        )


def test_canonical_arguments_digest_is_sha256_of_their_bytes():
    arguments = CanonicalArguments({"a": 1})
    assert arguments.digest == ArgumentsDigest(
        hashlib.sha256(arguments.data).hexdigest()
    )


def test_canonical_arguments_repr_does_not_expose_argument_values():
    arguments = CanonicalArguments({"secret": "application-data"})

    representation = repr(arguments)

    assert "application-data" not in representation
    assert arguments.digest.value in representation
    assert f"size={len(arguments.data)}" in representation
