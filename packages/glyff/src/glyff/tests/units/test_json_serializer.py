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

    def __glyff_identity__(self) -> str:
        return self._id

    def method(self, x: int):
        pass


class AnotherIdentityAware:
    _id = "class_id_456"

    def __glyff_identity__(self) -> str:
        return self._id

    @classmethod
    def class_method(cls, x: int):
        pass


class NoIdentity:
    def method(self, x: int):
        pass


class NonCallableIdentity:
    __glyff_identity__ = "not_a_method"

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
    import pytest

    with pytest.raises(TypeError, match="could not be serialized to JSON"):
        h.hash_args(func_with_obj, sig, (object(),), {})


def test_method_hash_differs_for_different_instances():
    inst1 = IdentityAware("id1")
    inst2 = IdentityAware("id2")
    sig = inspect.signature(inst1.method)

    h1 = h.hash_args(inst1.method, sig, (inst1, 10), {})
    h2 = h.hash_args(inst2.method, sig, (inst2, 10), {})

    assert h1 != h2


def test_method_hash_is_same_for_same_instance():
    inst = IdentityAware("id1")
    sig = inspect.signature(inst.method)

    h1 = h.hash_args(inst.method, sig, (inst, 10), {})
    h2 = h.hash_args(inst.method, sig, (inst, 10), {})

    assert h1 == h2


def test_class_method_hash_includes_class_identity():
    cls2 = AnotherIdentityAware
    sig2 = inspect.signature(cls2.class_method)

    h1 = h.hash_args(cls2.class_method, sig2, (cls2, 10), {})
    h2 = h.hash_args(
        AnotherIdentityAware.class_method, sig2, (AnotherIdentityAware, 10), {}
    )

    assert h1 == h2


def test_hash_method_on_class_without_identity_raises_type_error():
    inst = NoIdentity()
    sig = inspect.signature(inst.method)

    with pytest.raises(
        TypeError, match="does not implement a callable '__glyff_identity__' method"
    ):
        h.hash_args(inst.method, sig, (inst, 10), {})


def test_hash_method_on_class_with_non_callable_identity_raises_type_error():
    inst = NonCallableIdentity()
    sig = inspect.signature(inst.method)

    with pytest.raises(
        TypeError, match="does not implement a callable '__glyff_identity__' method"
    ):
        h.hash_args(inst.method, sig, (inst, 10), {})
