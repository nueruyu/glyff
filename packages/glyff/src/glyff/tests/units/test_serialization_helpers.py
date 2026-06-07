from glyff.serialization.helpers import stable_json_dumps


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
