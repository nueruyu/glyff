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


def test_hash_ignores_compare_false_dataclass_fields(hasher: ArgsHasher):
    @dataclasses.dataclass
    class AgentWithDep:
        name: str
        counter: int = dataclasses.field(compare=False, default=0)

        def run(self, query: str):
            pass

    func = AgentWithDep.run
    sig = inspect.signature(func)

    # field(compare=False) is excluded from the hash via the shared dataclass logic.
    h1 = hasher.hash_args(func, sig, (AgentWithDep("a", counter=1), "q"), {})
    h2 = hasher.hash_args(func, sig, (AgentWithDep("a", counter=99), "q"), {})
    h3 = hasher.hash_args(func, sig, (AgentWithDep("b", counter=1), "q"), {})

    assert h1 == h2
    assert h1 != h3


def test_hash_model_with_nested_opaque_member(hasher: ArgsHasher):
    """A model holding an opaque (non-serializable) member hashes without raising.

    The model's serializable state differentiates calls while the opaque member is
    identified by its class instead of triggering a PydanticSerializationError.
    """
    from pydantic import ConfigDict

    class Tool:
        def __init__(self, n):
            self.n = n

    class Agent(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        name: str
        tool: Tool

        def run(self, query: str):
            pass

    func = Agent.run
    sig = inspect.signature(func)

    a1 = Agent(name="researcher", tool=Tool(1))
    a2 = Agent(name="researcher", tool=Tool(2))
    a3 = Agent(name="writer", tool=Tool(1))

    h1 = hasher.hash_args(func, sig, (a1, "hi"), {})
    h2 = hasher.hash_args(func, sig, (a2, "hi"), {})
    h3 = hasher.hash_args(func, sig, (a3, "hi"), {})

    # Opaque tool identified by class (h1 == h2), serializable state differentiates.
    assert h1 == h2
    assert h1 != h3


def test_model_set_field_is_sorted_for_stable_hashing():
    """A model's set field is emitted as a sorted list so the hash is process-stable.

    pydantic_core would otherwise emit the set in (hash-randomized) iteration order.
    """
    from glyff_pydantic._serialization import _model_to_hashable

    class M(BaseModel):
        tags: set

    dumped = _model_to_hashable(M(tags={"gamma", "alpha", "beta"}))
    assert dumped["tags"] == ["alpha", "beta", "gamma"]


def test_model_set_field_hash_is_content_based(hasher: ArgsHasher):
    class M(BaseModel):
        tags: set

    def f(a: object):
        pass

    sig = inspect.signature(f)
    h1 = hasher.hash_args(f, sig, (M(tags={1, 2, 3}),), {})
    h2 = hasher.hash_args(f, sig, (M(tags={3, 2, 1}),), {})
    h3 = hasher.hash_args(f, sig, (M(tags={4, 5, 6}),), {})

    assert h1 == h2
    assert h1 != h3
