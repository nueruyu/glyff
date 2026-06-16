import dataclasses
import inspect

import pytest

from glyff.exceptions import SerializationError
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


def test_hash_unserializable_arg_uses_class_qualified_name(hasher: JsonArgsHasher):
    def func_with_obj(a: object):
        pass

    sig = inspect.signature(func_with_obj)

    first = hasher.hash_args(func_with_obj, sig, (MyPlainClass("id1"),), {})
    second = hasher.hash_args(func_with_obj, sig, (MyPlainClass("id2"),), {})
    different = hasher.hash_args(func_with_obj, sig, (object(),), {})

    # Unserializable values are identified by their class, so instances of the same
    # class hash identically (state is intentionally ignored) while instances of a
    # different class differ.
    assert first == second
    assert first != different


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


def test_hash_method_on_plain_class_uses_class_qualified_name(hasher: JsonArgsHasher):
    func = MyPlainClass.method
    sig = inspect.signature(func)

    # A plain (non-dataclass) `self` is hashed by its class qualified name, so calls on
    # different instances of the same class collapse to the same hash while arguments
    # still differentiate the call.
    h1 = hasher.hash_args(func, sig, (MyPlainClass("id1"), 10), {})
    h2 = hasher.hash_args(func, sig, (MyPlainClass("id2"), 10), {})
    h3 = hasher.hash_args(func, sig, (MyPlainClass("id1"), 20), {})

    assert h1 == h2
    assert h1 != h3


def test_hash_dataclass_with_nested_plain_service(hasher: JsonArgsHasher):
    """A dataclass (state matters) holding plain, non-deepcopyable services hashes.

    The dataclass state differentiates calls while nested plain services are identified
    by their class, even when they hold members that cannot be deep-copied.
    """
    import threading

    class Tool:
        def __init__(self, lock):
            self.lock = lock

    @dataclasses.dataclass
    class Agent:
        name: str
        tools: list

        def run(self, query: str):
            pass

    func = Agent.run
    sig = inspect.signature(func)

    a1 = Agent("researcher", [Tool(threading.Lock())])
    a2 = Agent("researcher", [Tool(threading.Lock())])
    a3 = Agent("writer", [Tool(threading.Lock())])

    h1 = hasher.hash_args(func, sig, (a1, "hi"), {})
    h2 = hasher.hash_args(func, sig, (a2, "hi"), {})
    h3 = hasher.hash_args(func, sig, (a3, "hi"), {})

    assert h1 == h2
    assert h1 != h3


def test_hash_set_is_by_content_and_order_independent(hasher: JsonArgsHasher):
    def func(a: set):
        pass

    sig = inspect.signature(func)

    # Value types json doesn't encode natively (set/frozenset) are hashed by content,
    # independent of insertion order, rather than colliding on "builtins.set".
    h_ab = hasher.hash_args(func, sig, ({1, 2, 3},), {})
    h_ba = hasher.hash_args(func, sig, ({3, 2, 1},), {})
    h_cd = hasher.hash_args(func, sig, ({4, 5, 6},), {})

    assert h_ab == h_ba
    assert h_ab != h_cd


def test_hash_frozenset_matches_equivalent_set(hasher: JsonArgsHasher):
    def func(a: object):
        pass

    sig = inspect.signature(func)
    h_set = hasher.hash_args(func, sig, ({1, 2},), {})
    h_frozen = hasher.hash_args(func, sig, (frozenset({2, 1}),), {})

    # Both reduce to the same sorted content representation.
    assert h_set == h_frozen


def test_hash_bytes_is_by_content(hasher: JsonArgsHasher):
    def func(a: bytes):
        pass

    sig = inspect.signature(func)
    h1 = hasher.hash_args(func, sig, (b"abc",), {})
    h2 = hasher.hash_args(func, sig, (b"abc",), {})
    h3 = hasher.hash_args(func, sig, (b"xyz",), {})

    assert h1 == h2
    assert h1 != h3


def test_regular_function_with_self_parameter_is_hashed_normally(
    hasher: JsonArgsHasher,
):
    def regular_func(self: str, x: int):
        pass

    sig = inspect.signature(regular_func)
    result = hasher.hash_args(regular_func, sig, ("a serializable string", 10), {})
    assert result is not None
