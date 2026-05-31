import inspect

import pytest

from glyff.serialization import JsonArgsHasher, JsonSerializer

s = JsonSerializer()
h = JsonArgsHasher()


def sample_func(a: int, b: str = "default"):
    pass


class IdentityAware:
    def __init__(self, id_val: str):
        self._id = id_val

    @property
    def __glyff_identity__(self) -> str:
        return self._id

    def method(self, x: int):
        pass


class AnotherIdentityAware:
    __glyff_identity__ = "class_id_456"

    @classmethod
    def class_method(cls, x: int):
        pass


class NoIdentity:
    def method(self, x: int):
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

    with pytest.raises(TypeError, match="could not be serialized to JSON"):
        h.hash_args(func_with_obj, sig, (object(),), {})


def test_method_hash_differs_for_different_instances():
    inst1 = IdentityAware("id1")
    inst2 = IdentityAware("id2")
    func = IdentityAware.method
    sig = inspect.signature(func)

    h1 = h.hash_args(func, sig, (inst1, 10), {})
    h2 = h.hash_args(func, sig, (inst2, 10), {})

    assert h1 != h2


def test_method_hash_is_same_for_same_instance():
    inst = IdentityAware("id1")
    func = IdentityAware.method
    sig = inspect.signature(func)

    h1 = h.hash_args(func, sig, (inst, 10), {})
    h2 = h.hash_args(func, sig, (inst, 10), {})

    assert h1 == h2


def test_class_method_hash_includes_class_identity():
    func = AnotherIdentityAware.class_method.__func__
    sig = inspect.signature(func)

    h1 = h.hash_args(func, sig, (AnotherIdentityAware, 10), {})
    h2 = h.hash_args(func, sig, (AnotherIdentityAware, 10), {})

    assert h1 == h2


def test_hash_method_on_class_without_identity_raises_type_error():
    inst = NoIdentity()
    func = NoIdentity.method
    sig = inspect.signature(func)

    with pytest.raises(TypeError, match="does not implement the Identifiable protocol"):
        h.hash_args(func, sig, (inst, 10), {})


def test_regular_function_with_self_parameter_does_not_raise():
    def regular_func(self, x: int):
        pass

    sig = inspect.signature(regular_func)
    result = h.hash_args(regular_func, sig, ("not_an_instance", 10), {})
    assert result is not None
