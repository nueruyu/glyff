import dataclasses
import functools
import hashlib

import pytest

from glyff import ArgumentsDigest, CanonicalArguments, CanonicalValue
from glyff.exceptions import ArgumentCanonicalizationError
from glyff.serialization import Opaque, OpaqueByTypeQualname
from glyff.serialization._utils import (
    _canonicalize_set,
    encode_canonical,
    stable_json_dumps,
    to_canonical,
)


@dataclasses.dataclass
class _TagField:
    __glyff_opaque__: str


def canonical(obj: object, **kwargs) -> CanonicalValue:
    return to_canonical(obj, **kwargs)


def test_sorted_canonical_is_stable_for_partial_order_elements():
    # frozenset only defines a partial order, so a direct sorted() would leave
    # incomparable elements in process-randomized input order. _canonicalize_set must
    # instead order them by their encoded form.
    values = {frozenset({"a", "b"}), frozenset({"c", "d"}), frozenset({"e"})}
    ordered = _canonicalize_set(values, to_canonical)
    assert ordered == sorted(ordered, key=encode_canonical)


def test_stable_json_dumps_defaults_to_compact_readable_json():
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
    # Strings, not small ints: a small int set iterates in sorted order anyway,
    # so it would pass whether or not anything sorted it.
    assert canonical({"t": (1, 2), "s": {"gamma", "alpha", "beta"}}) == {
        "t": [1, 2],
        "s": ["alpha", "beta", "gamma"],
    }


def test_canonical_gives_a_frozenset_the_form_of_its_set():
    assert canonical(frozenset({2, 1})) == canonical({1, 2})


def test_canonical_encodes_bytes_as_hex():
    assert canonical(b"\x01\xff") == "01ff"


def test_canonical_keeps_only_compared_dataclass_fields():
    @dataclasses.dataclass
    class D:
        kept: int
        ignored: int = dataclasses.field(default=0, compare=False)

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
    # Coerced here, not by the encoder, so a JSON round trip reproduces the bytes.
    assert canonical({2: "a", 10: "b"}) == {"2": "a", "10": "b"}
    assert encode_canonical(canonical({2: "a", 10: "b"})) == b'{"10":"b","2":"a"}'


def test_canonical_rejects_keys_that_collide_once_stringified():
    # 1 and "1" would collapse into one entry, silently keying two different calls
    # the same way.
    with pytest.raises(ArgumentCanonicalizationError, match="canonicalize to"):
        canonical({1: "integer", "1": "string"})

    with pytest.raises(ArgumentCanonicalizationError, match="canonicalize to"):
        canonical({True: "boolean", "true": "string"})


def test_canonical_renders_str_subclass_keys_as_their_builtin():
    class CustomKey(str):
        def __str__(self) -> str:
            return "rewritten"

    # json keys the entry by the string's own data, so identity must too.
    assert canonical({CustomKey("actual"): 1}) == {"actual": 1}


def test_canonical_renders_int_subclass_keys_as_their_builtin():
    class Port(int):
        def __repr__(self) -> str:
            return f"Port({int(self)})"

    assert canonical({Port(8080): "x"}) == {"8080": "x"}


def test_canonical_rejects_unrepresentable_mapping_keys():
    with pytest.raises(ArgumentCanonicalizationError, match="Dictionary keys"):
        canonical({(1, 2): "a"})


def test_canonical_rejects_opaque_values_by_default():
    class Service:
        pass

    with pytest.raises(ArgumentCanonicalizationError, match="no value representation"):
        canonical(Service())


def test_canonical_tags_policy_output_so_it_cannot_collide():
    class Service:
        pass

    tagged = canonical(Service(), policy=OpaqueByTypeQualname())
    assert tagged != canonical(f"{__name__}.test_canonical_tags_policy_output.Service")
    assert list(tagged) == ["__glyff_opaque__"]  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [{"__glyff_opaque__": "x"}, _TagField("x")])
def test_canonical_refuses_a_value_that_claims_the_policy_tag(value):
    # The marker is how a value with no representation is written down. Anything
    # else canonicalizing to it would share that value's key, whichever branch
    # of the walk built the mapping — a native one or a dataclass's fields.
    with pytest.raises(ArgumentCanonicalizationError, match="reserved"):
        canonical(value)


def test_canonical_keeps_a_recorded_opaque_value_as_its_marker():
    class Service:
        pass

    recorded = canonical(Service(), policy=OpaqueByTypeQualname())

    assert canonical(Opaque(f"{__name__}.{Service.__qualname__}")) == recorded


def test_canonical_applies_the_policy_at_any_depth():
    class Service:
        pass

    assert canonical({"a": [Service()]}, policy=OpaqueByTypeQualname()) == {
        "a": [{"__glyff_opaque__": f"{__name__}.{Service.__qualname__}"}]
    }


def test_encode_canonical_sorts_keys_and_stays_compact():
    assert encode_canonical({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'


def test_encode_canonical_rejects_values_outside_the_json_data_model():
    with pytest.raises(
        ArgumentCanonicalizationError, match="not in the JSON data model"
    ):
        encode_canonical({"a": {1, 2}})  # type: ignore[dict-item]


def test_encoded_arguments_digest_is_sha256_of_their_bytes():
    data = encode_canonical({"a": 1})
    assert CanonicalArguments(data).digest == ArgumentsDigest(
        hashlib.sha256(data).hexdigest()
    )
