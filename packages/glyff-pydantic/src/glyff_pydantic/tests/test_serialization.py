import dataclasses
import inspect

import pytest
from glyff.exceptions import SerializationError
from glyff import ArgsHasher, Serializer
from pydantic import BaseModel

from glyff_pydantic import PydanticArgsHasher, PydanticSerializer


class MyModel(BaseModel):
    x: int
    y: str

    def method(self, z: int):
        pass


@dataclasses.dataclass(frozen=True)
class MyDataClass:
    id: str
    value: int


class MyPlainClass:
    pass


def helper_func():
    pass


def another_helper_func():
    pass


def sample_func(a: int, b: str = "default"):
    pass


@pytest.fixture
def serializer() -> Serializer:
    return PydanticSerializer()


@pytest.fixture
def hasher() -> ArgsHasher:
    return PydanticArgsHasher()


def test_hash_args_positional_vs_keyword_are_equal(hasher: ArgsHasher):
    sig = inspect.signature(sample_func)
    h1 = hasher.hash_args(sample_func, sig, (1,), {"b": "test"})
    h2 = hasher.hash_args(sample_func, sig, (), {"a": 1, "b": "test"})
    assert h1 == h2


def test_hash_args_defaults_are_included(hasher: ArgsHasher):
    sig = inspect.signature(sample_func)
    h1 = hasher.hash_args(sample_func, sig, (1,), {})
    h2 = hasher.hash_args(sample_func, sig, (), {"a": 1, "b": "default"})
    assert h1 == h2


def test_hash_args_different_values_differ(hasher: ArgsHasher):
    sig = inspect.signature(sample_func)
    h1 = hasher.hash_args(sample_func, sig, (1,), {})
    h2 = hasher.hash_args(sample_func, sig, (2,), {})
    assert h1 != h2


def test_hash_args_is_deterministic(hasher: ArgsHasher):
    sig = inspect.signature(sample_func)
    h1 = hasher.hash_args(sample_func, sig, (42,), {"b": "hello"})
    h2 = hasher.hash_args(sample_func, sig, (42,), {"b": "hello"})
    assert h1 == h2


async def test_serialize_deserialize_primitives(serializer: Serializer):
    data = {"key": "value", "num": 123, "flag": True, "items": [1, "a"]}
    serialized = await serializer.serialize(data, dict)
    assert await serializer.deserialize(serialized, dict) == data


async def test_serialize_deserialize_pydantic_model(serializer: Serializer):
    model = MyModel(x=42, y="hello")
    serialized = await serializer.serialize(model, MyModel)
    assert await serializer.deserialize(serialized, MyModel) == model


async def test_serialize_deserialize_list_of_models(serializer: Serializer):
    models = [MyModel(x=i, y=str(i)) for i in range(3)]
    serialized = await serializer.serialize(models, list[MyModel])
    result = await serializer.deserialize(serialized, list[MyModel])
    assert result == models


async def test_serialize_produces_stable_output(serializer: Serializer):
    d1 = {"b": 2, "a": 1}
    d2 = {"a": 1, "b": 2}
    assert await serializer.serialize(d1, dict) == await serializer.serialize(d2, dict)


async def test_serialize_defaults_to_compact_ascii_json():
    serialized = await PydanticSerializer().serialize({"message": "こんにちは"}, dict)
    assert serialized == b'{"message":"\\u3053\\u3093\\u306b\\u3061\\u306f"}'


async def test_serialize_accepts_json_formatting_options():
    serialized = await PydanticSerializer(indent=2, ensure_ascii=False).serialize(
        {"message": "こんにちは"}, dict
    )

    assert serialized.decode("utf-8") == '{\n  "message": "こんにちは"\n}'


async def test_serialize_accepts_string_indent():
    serialized = await PydanticSerializer(indent="\t").serialize(
        {"message": "hello"}, dict
    )

    assert serialized.decode("utf-8") == '{\n\t"message": "hello"\n}'


async def test_serialize_non_serializable_raises_custom_error(serializer: Serializer):
    with pytest.raises(SerializationError, match="could not be serialized"):
        await serializer.serialize(object(), object)


def test_hash_unserializable_arg_uses_class_qualified_name(hasher: ArgsHasher):
    class PlainA:
        pass

    class PlainB:
        pass

    def func_with_obj(a: object):
        pass

    sig = inspect.signature(func_with_obj)

    first = hasher.hash_args(func_with_obj, sig, (PlainA(),), {})
    second = hasher.hash_args(func_with_obj, sig, (PlainA(),), {})
    different = hasher.hash_args(func_with_obj, sig, (PlainB(),), {})

    # Unserializable values are identified by their class qualified name: same class
    # hashes identically, different class differs.
    assert first == second
    assert first != different


def test_hash_nested_dataclass_type_and_callable_values(hasher: ArgsHasher):
    @dataclasses.dataclass(frozen=True)
    class Container:
        data: MyDataClass
        cls: type
        callback: object

    def func(container: Container):
        pass

    sig = inspect.signature(func)
    first = hasher.hash_args(
        func,
        sig,
        (
            Container(
                data=MyDataClass(id="id1", value=100),
                cls=MyPlainClass,
                callback=helper_func,
            ),
        ),
        {},
    )
    second = hasher.hash_args(
        func,
        sig,
        (
            Container(
                data=MyDataClass(id="id1", value=100),
                cls=MyPlainClass,
                callback=helper_func,
            ),
        ),
        {},
    )

    assert first == second


def test_hash_callable_values_by_qualified_name(hasher: ArgsHasher):
    def func(callback):
        pass

    sig = inspect.signature(func)
    first = hasher.hash_args(func, sig, (helper_func,), {})
    second = hasher.hash_args(func, sig, (helper_func,), {})
    different = hasher.hash_args(func, sig, (another_helper_func,), {})

    assert first == second
    assert first != different


def test_method_hash_differs_for_different_pydantic_instances(hasher: ArgsHasher):
    inst1 = MyModel(x=1, y="a")
    inst2 = MyModel(x=2, y="a")
    func = MyModel.method
    sig = inspect.signature(func)

    h1 = hasher.hash_args(func, sig, (inst1, 10), {})
    h2 = hasher.hash_args(func, sig, (inst2, 10), {})

    assert h1 != h2


def test_method_hash_is_same_for_identical_pydantic_instances(hasher: ArgsHasher):
    inst1 = MyModel(x=1, y="a")
    inst2 = MyModel(x=1, y="a")
    func = MyModel.method
    sig = inspect.signature(func)

    h1 = hasher.hash_args(func, sig, (inst1, 10), {})
    h2 = hasher.hash_args(func, sig, (inst2, 10), {})

    assert h1 == h2
