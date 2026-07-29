import dataclasses
import functools
import hashlib

import pytest

from glyff import CanonicalValue
from glyff.exceptions import UnserializableArgumentError
from glyff.serialization import QualnameOpaque
from glyff.serialization._utils import (
    _sorted_canonical,
    args_digest,
    encode_canonical,
    stable_json_dumps,
    to_canonical,
)


def canonical(obj: object, **kwargs) -> CanonicalValue:
    return to_canonical(obj, **kwargs)


def test_sorted_canonical_is_stable_for_partial_order_elements():
    # frozenset only defines a partial order, so a direct sorted() would leave
    # incomparable elements in process-randomized input order. _sorted_canonical must
    # instead order them by their encoded form.
    values = {frozenset({"a", "b"}), frozenset({"c", "d"}), frozenset({"e"})}
    ordered = _sorted_canonical(values, to_canonical)
    assert ordered == sorted(ordered, key=encode_canonical)


def test_stable_json_dumps_defaults_to_compact_readable_json():
    # Non-ASCII is emitted as itself: recorded arguments are read by whoever writes
    # a migration, and every store already wrote JSON this way.
    assert stable_json_dumps({"message": "こんにちは"}) == '{"message":"こんにちは"}'


def test_stable_json_dumps_accepts_json_formatting_options():
    assert stable_json_dumps(
        {"message": "こんにちは"}, indent=2, ensure_ascii=True
    ) == ('{\n  "message": "\\u3053\\u3093\\u306b\\u3061\\u306f"\n}')


def test_stable_json_dumps_accepts_string_indent():
    assert stable_json_dumps({"message": "hello"}, indent="\t") == (
        '{\n\t"message": "hello"\n}'
    )


def test_canonical_passes_through_json_native_values():
    assert canonical({"a": 1, "b": "x", "c": True, "d": None, "e": 1.5}) == {
        "a": 1,
        "b": "x",
        "c": True,
        "d": None,
        "e": 1.5,
    }


def test_canonical_normalizes_containers():
    # Tuples and sets both become lists; a set is ordered so the form is stable.
    assert canonical({"t": (1, 2), "s": {3, 1, 2}}) == {"t": [1, 2], "s": [1, 2, 3]}


def test_canonical_encodes_bytes_as_hex():
    assert canonical(b"\x01\xff") == "01ff"


def test_canonical_keeps_only_compared_dataclass_fields():
    @dataclasses.dataclass
    class D:
        kept: int
        ignored: int = dataclasses.field(default=0, compare=False)

    # A field the dataclass excludes from equality never distinguished two calls,
    # so it is not part of the canonical form either.
    assert canonical(D(kept=1, ignored=99)) == {"kept": 1}


def test_canonical_identifies_types_and_functions_by_qualified_name():
    def helper():
        pass

    assert canonical(helper) == f"{__name__}.{helper.__qualname__}"
    assert canonical(int) == "builtins.int"


def test_canonical_decomposes_partials():
    def helper(a, b):
        pass

    assert canonical(functools.partial(helper, 1, b=2)) == {
        "__partial__": f"{__name__}.{helper.__qualname__}",
        "args": [1],
        "keywords": {"b": 2},
    }


def test_canonical_coerces_non_string_mapping_keys():
    # Keys are coerced here rather than by the encoder, so reading the encoded form
    # back and re-encoding it reproduces the same bytes (json orders by the original
    # key, which would put "2" before "10" only until the first round trip).
    assert canonical({2: "a", 10: "b"}) == {"2": "a", "10": "b"}
    assert encode_canonical(canonical({2: "a", 10: "b"})) == b'{"10":"b","2":"a"}'


def test_canonical_rejects_unrepresentable_mapping_keys():
    with pytest.raises(UnserializableArgumentError, match="Dictionary keys"):
        canonical({(1, 2): "a"})


def test_canonical_rejects_opaque_values_by_default():
    class Service:
        pass

    with pytest.raises(UnserializableArgumentError, match="no value representation"):
        canonical(Service())


def test_canonical_tags_policy_output_so_it_cannot_collide():
    class Service:
        pass

    tagged = canonical(Service(), policy=QualnameOpaque())
    # The policy's output is namespaced, so an opaque value can never match a plain
    # string argument that happens to be the same qualified name.
    assert tagged != canonical(f"{__name__}.test_canonical_tags_policy_output.Service")
    assert list(tagged) == ["__glyff_opaque__"]  # type: ignore[arg-type]


def test_canonical_applies_the_policy_at_any_depth():
    class Service:
        pass

    assert canonical({"a": [Service()]}, policy=QualnameOpaque()) == {
        "a": [{"__glyff_opaque__": f"{__name__}.{Service.__qualname__}"}]
    }


def test_encode_canonical_sorts_keys_and_stays_compact():
    assert encode_canonical({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'


def test_encode_canonical_rejects_values_outside_the_json_data_model():
    with pytest.raises(UnserializableArgumentError, match="not in the JSON data model"):
        encode_canonical({"a": {1, 2}})  # type: ignore[dict-item]


def test_args_digest_is_sha256_of_the_encoded_bytes():
    # The invariant every recorded execution carries: the key's args_hash is the
    # digest of exactly the bytes stored as its arguments.
    data = encode_canonical({"a": 1})
    assert args_digest(data) == hashlib.sha256(data).hexdigest()
