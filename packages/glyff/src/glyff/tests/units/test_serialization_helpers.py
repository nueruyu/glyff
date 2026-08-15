import dataclasses
import functools
from typing import Any

import pytest

from glyff import (
    CanonicalArguments,
    CanonicalArgumentValue,
    CanonicalFallback,
)
from glyff.exceptions import ArgumentCanonicalizationError
from glyff.serialization import (
    CanonicalFallbackRepresenter,
    FallbackByTypeQualname,
)
from glyff.serialization._canonicalization import (
    canonicalize_set,
    to_canonical,
)
from glyff.serialization._fallback import fallback_representer_or_reject
from glyff.serialization._utils import stable_json_dumps


@dataclasses.dataclass
class _TagField:
    __glyff_fallback__: str


def canonical(
    obj: object,
    fallback_representer: CanonicalFallbackRepresenter | None = None,
) -> CanonicalArgumentValue:
    return to_canonical(obj, fallback_representer_or_reject(fallback_representer))


def test_sorted_canonical_is_stable_for_partial_order_elements():
    # frozenset only defines a partial order, so a direct sorted() would leave
    # incomparable elements in process-randomized input order. _canonicalize_set must
    # instead order them by their encoded form.
    values = {frozenset({"a", "b"}), frozenset({"c", "d"}), frozenset({"e"})}
    ordered = canonicalize_set(values, canonical)
    assert ordered == sorted(
        ordered, key=lambda value: CanonicalArguments({"value": value}).data
    )


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
    def helper(a: Any, b: Any) -> None:
        pass

    assert canonical(functools.partial(helper, 1, b=2)) == {
        "__partial__": f"{__name__}.{helper.__qualname__}",
        "args": [1],
        "keywords": {"b": 2},
    }


def test_canonical_coerces_non_string_mapping_keys():
    # Coerced here, not by the encoder, so a JSON round trip reproduces the bytes.
    arguments = canonical({2: "a", 10: "b"})
    assert arguments == {"2": "a", "10": "b"}
    assert isinstance(arguments, dict)
    assert CanonicalArguments(arguments).data == b'{"10":"b","2":"a"}'


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


def test_canonical_rejects_values_without_a_representation_by_default():
    class Service:
        pass

    with pytest.raises(
        ArgumentCanonicalizationError, match="no canonical representation"
    ):
        canonical(Service())


def test_canonical_tags_fallback_output_so_it_cannot_collide():
    class Service:
        pass

    tagged = canonical(Service(), fallback_representer=FallbackByTypeQualname())
    assert tagged != canonical(f"{Service.__module__}.{Service.__qualname__}")
    assert tagged == CanonicalFallback(f"{Service.__module__}.{Service.__qualname__}")


@pytest.mark.parametrize("value", [{"__glyff_fallback__": "x"}, _TagField("x")])
def test_canonical_refuses_a_value_that_claims_the_fallback_tag(value: Any) -> None:
    # The marker is how a value with no representation is written down. Anything
    # else canonicalizing to it would share that value's key, whichever branch
    # of the walk built the mapping — a native one or a dataclass's fields.
    with pytest.raises(ArgumentCanonicalizationError, match="reserved"):
        CanonicalArguments({"value": canonical(value)})


def test_canonical_keeps_a_recorded_fallback_as_its_marker():
    class Service:
        pass

    recorded = canonical(Service(), fallback_representer=FallbackByTypeQualname())

    assert (
        canonical(CanonicalFallback(f"{__name__}.{Service.__qualname__}")) == recorded
    )


def test_canonical_applies_the_fallback_at_any_depth():
    class Service:
        pass

    assert canonical(
        {"a": [Service()]}, fallback_representer=FallbackByTypeQualname()
    ) == {"a": [CanonicalFallback(f"{__name__}.{Service.__qualname__}")]}


def test_a_fallback_representer_must_return_a_canonical_value():
    class InvalidFallback(CanonicalFallbackRepresenter):
        def represent(self, value: Any) -> CanonicalArgumentValue:
            return object()  # type: ignore[return-value]

    with pytest.raises(
        ArgumentCanonicalizationError, match="not in the JSON data model"
    ):
        canonical(object(), fallback_representer=InvalidFallback())
