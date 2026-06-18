from glyff.serialization._utils import _sorted_for_hash, stable_json_dumps, to_hashable


def test_sorted_for_hash_is_stable_for_partial_order_elements():
    # frozenset only defines a partial order, so a direct sorted() would leave
    # incomparable elements in process-randomized input order. _sorted_for_hash must
    # instead order them by their canonical JSON.
    values = {frozenset({"a", "b"}), frozenset({"c", "d"}), frozenset({"e"})}
    expected = sorted(values, key=lambda v: stable_json_dumps(v, default=to_hashable))
    assert _sorted_for_hash(values) == expected


def test_stable_json_dumps_defaults_to_compact_ascii_json():
    assert stable_json_dumps({"message": "こんにちは"}) == (
        '{"message":"\\u3053\\u3093\\u306b\\u3061\\u306f"}'
    )


def test_stable_json_dumps_accepts_json_formatting_options():
    assert stable_json_dumps(
        {"message": "こんにちは"}, indent=2, ensure_ascii=False
    ) == ('{\n  "message": "こんにちは"\n}')


def test_stable_json_dumps_accepts_string_indent():
    assert stable_json_dumps({"message": "hello"}, indent="\t") == (
        '{\n\t"message": "hello"\n}'
    )
