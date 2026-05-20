import inspect

from glyff.serialization import JsonArgsHasher, JsonSerializer

s = JsonSerializer()
h = JsonArgsHasher()


def sample_func(a: int, b: str = "default"):
    pass


def test_hash_args_positional_vs_keyword_are_equal():
    sig = inspect.signature(sample_func)
    h1 = h.hash_args(sample_func, sig, (1,), {"b": "test"})
    h2 = h.hash_args(sample_func, sig, (), {"a": 1, "b": "test"})
    assert h1 == h2


def test_hash_args_defaults_are_included():
    sig = inspect.signature(sample_func)
    h1 = h.hash_args(sample_func, sig, (1,), {})
    h2 = h.hash_args(sample_func, sig, (), {"a": 1, "b": "default"})
    assert h1 == h2


def test_hash_args_different_values_differ():
    sig = inspect.signature(sample_func)
    h1 = h.hash_args(sample_func, sig, (1,), {})
    h2 = h.hash_args(sample_func, sig, (2,), {})
    assert h1 != h2


def test_hash_args_is_deterministic():
    sig = inspect.signature(sample_func)
    h1 = h.hash_args(sample_func, sig, (42,), {"b": "hello"})
    h2 = h.hash_args(sample_func, sig, (42,), {"b": "hello"})
    assert h1 == h2


def test_serialize_deserialize_primitives():
    data = {"key": "value", "num": 123, "flag": True, "items": [1, "a"]}
    assert s.deserialize(s.serialize(data, dict), dict) == data


def test_serialize_produces_stable_output():
    d1 = {"b": 2, "a": 1}
    d2 = {"a": 1, "b": 2}
    assert s.serialize(d1, dict) == s.serialize(d2, dict)


def test_hash_non_serializable_raises_type_error():
    def func_with_obj(a: object):
        pass

    sig = inspect.signature(func_with_obj)
    import pytest

    with pytest.raises(TypeError, match="could not be serialized to JSON"):
        h.hash_args(func_with_obj, sig, (object(),), {})
