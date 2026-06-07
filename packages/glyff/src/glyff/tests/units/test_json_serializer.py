import dataclasses
import inspect

import pytest

from glyff.exceptions import SerializationError, UnserializableArgumentError
from glyff.serialization import JsonArgsHasher, JsonSerializer


def sample_func(a: int, b: str = "default"):
    pass


@dataclasses.dataclass(frozen=True)
class MyDataClass:
    id: str
    value: int

    def method(self, x: int):
        pass


class MyPlainClass:
    def __init__(self, id_val: str):
        self.id = id_val

    def method(self, x: int):
        pass


class AnotherClass:
    @classmethod
    def class_method(cls, x: int):
        pass


def helper_func():
    pass


def another_helper_func():
    pass


@pytest.fixture
def serializer() -> JsonSerializer:
    return JsonSerializer()


@pytest.fixture
def hasher() -> JsonArgsHasher:
    return JsonArgsHasher()


def test_hash_args_positional_vs_keyword_are_equal(hasher: JsonArgsHasher):
    sig = inspect.signature(sample_func)
    h1 = hasher.hash_args(sample_func, sig, (1,), {"b": "test"})
    h2 = hasher.hash_args(sample_func, sig, (), {"a": 1, "b": "test"})
    assert h1 == h2


def test_hash_args_defaults_are_included(hasher: JsonArgsHasher):
    sig = inspect.signature(sample_func)
    h1 = hasher.hash_args(sample_func, sig, (1,), {})
    h2 = hasher.hash_args(sample_func, sig, (), {"a": 1, "b": "default"})
    assert h1 == h2


def test_hash_args_different_values_differ(hasher: JsonArgsHasher):
    sig = inspect.signature(sample_func)
    h1 = hasher.hash_args(sample_func, sig, (1,), {})
    h2 = hasher.hash_args(sample_func, sig, (2,), {})
    assert h1 != h2


def test_hash_args_is_deterministic(hasher: JsonArgsHasher):
    sig = inspect.signature(sample_func)
    h1 = hasher.hash_args(sample_func, sig, (42,), {"b": "hello"})
    h2 = hasher.hash_args(sample_func, sig, (42,), {"b": "hello"})
    assert h1 == h2


async def test_serialize_deserialize_primitives(serializer: JsonSerializer):
    data = {"key": "value", "num": 123, "flag": True, "items": [1, "a"]}
    serialized = await serializer.serialize(data, dict)
    assert await serializer.deserialize(serialized, dict) == data


async def test_serialize_produces_stable_output(serializer: JsonSerializer):
    d1 = {"b": 2, "a": 1}
    d2 = {"a": 1, "b": 2}
    assert await serializer.serialize(d1, dict) == await serializer.serialize(d2, dict)


async def test_serialize_non_serializable_raises_custom_error(
    serializer: JsonSerializer,
):
    with pytest.raises(SerializationError, match="could not be serialized to JSON"):
        await serializer.serialize(object(), object)


def test_hash_non_serializable_raises_custom_error(hasher: JsonArgsHasher):
    def func_with_obj(a: object):
        pass

    sig = inspect.signature(func_with_obj)

    with pytest.raises(
        UnserializableArgumentError, match="could not be serialized to JSON"
    ):
        hasher.hash_args(func_with_obj, sig, (object(),), {})


def test_hash_nested_dataclass_and_type_values(hasher: JsonArgsHasher):
    @dataclasses.dataclass(frozen=True)
    class Container:
        data: MyDataClass
        cls: type

    def func(container: Container):
        pass

    sig = inspect.signature(func)
    first = hasher.hash_args(
        func,
        sig,
        (Container(data=MyDataClass(id="id1", value=100), cls=AnotherClass),),
        {},
    )
    second = hasher.hash_args(
        func,
        sig,
        (Container(data=MyDataClass(id="id1", value=100), cls=AnotherClass),),
        {},
    )

    assert first == second


def test_hash_callable_values_by_qualified_name(hasher: JsonArgsHasher):
    def func(callback):
        pass

    sig = inspect.signature(func)
    first = hasher.hash_args(func, sig, (helper_func,), {})
    second = hasher.hash_args(func, sig, (helper_func,), {})
    different = hasher.hash_args(func, sig, (another_helper_func,), {})

    assert first == second
    assert first != different


def test_method_hash_differs_for_different_dataclass_instances(
    hasher: JsonArgsHasher,
):
    inst1 = MyDataClass(id="id1", value=100)
    inst2 = MyDataClass(id="id2", value=100)
    func = MyDataClass.method
    sig = inspect.signature(func)

    h1 = hasher.hash_args(func, sig, (inst1, 10), {})
    h2 = hasher.hash_args(func, sig, (inst2, 10), {})

    assert h1 != h2


def test_method_hash_is_same_for_identical_dataclass_instances(
    hasher: JsonArgsHasher,
):
    inst1 = MyDataClass(id="id1", value=100)
    inst2 = MyDataClass(id="id1", value=100)
    func = MyDataClass.method
    sig = inspect.signature(func)

    h1 = hasher.hash_args(func, sig, (inst1, 10), {})
    h2 = hasher.hash_args(func, sig, (inst2, 10), {})

    assert h1 == h2


def test_class_method_hash_is_stable(hasher: JsonArgsHasher):
    func = AnotherClass.class_method.__func__
    sig = inspect.signature(func)

    h1 = hasher.hash_args(func, sig, (AnotherClass, 10), {})
    h2 = hasher.hash_args(func, sig, (AnotherClass, 10), {})

    assert h1 == h2


def test_hash_method_on_unserializable_class_raises_error(hasher: JsonArgsHasher):
    inst = MyPlainClass("id1")
    func = MyPlainClass.method
    sig = inspect.signature(func)

    with pytest.raises(UnserializableArgumentError):
        hasher.hash_args(func, sig, (inst, 10), {})


def test_regular_function_with_self_parameter_is_hashed_normally(
    hasher: JsonArgsHasher,
):
    def regular_func(self: str, x: int):
        pass

    sig = inspect.signature(regular_func)
    result = hasher.hash_args(regular_func, sig, ("a serializable string", 10), {})
    assert result is not None
