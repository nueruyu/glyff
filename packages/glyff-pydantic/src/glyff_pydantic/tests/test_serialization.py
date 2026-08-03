import dataclasses
import datetime
import inspect
import uuid
from typing import Any

import pytest
from glyff.exceptions import SerializationError, ArgumentCanonicalizationError
from glyff import ArgsCanonicalizer, Serializer
from pydantic import BaseModel

from glyff_pydantic import PydanticArgsCanonicalizer, PydanticSerializer


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
def canonicalizer() -> ArgsCanonicalizer:
    return PydanticArgsCanonicalizer()


def test_canonicalize_args_positional_vs_keyword_are_equal(
    canonicalizer: ArgsCanonicalizer,
):
    sig = inspect.signature(sample_func)
    first = canonicalizer.canonicalize_args(sample_func, sig, (1,), {"b": "test"})
    second = canonicalizer.canonicalize_args(
        sample_func, sig, (), {"a": 1, "b": "test"}
    )
    assert first == second


def test_canonicalize_args_defaults_are_included(canonicalizer: ArgsCanonicalizer):
    sig = inspect.signature(sample_func)
    first = canonicalizer.canonicalize_args(sample_func, sig, (1,), {})
    second = canonicalizer.canonicalize_args(
        sample_func, sig, (), {"a": 1, "b": "default"}
    )
    assert first == second


def test_canonicalize_args_different_values_differ(canonicalizer: ArgsCanonicalizer):
    sig = inspect.signature(sample_func)
    first = canonicalizer.canonicalize_args(sample_func, sig, (1,), {})
    second = canonicalizer.canonicalize_args(sample_func, sig, (2,), {})
    assert first != second


def test_canonicalize_args_is_deterministic(canonicalizer: ArgsCanonicalizer):
    sig = inspect.signature(sample_func)
    first = canonicalizer.canonicalize_args(sample_func, sig, (42,), {"b": "hello"})
    second = canonicalizer.canonicalize_args(sample_func, sig, (42,), {"b": "hello"})
    assert first == second


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


async def test_serialize_defaults_to_compact_readable_json():
    serialized = await PydanticSerializer().serialize({"message": "こんにちは"}, dict)
    assert serialized.decode("utf-8") == '{"message":"こんにちは"}'


async def test_serialize_accepts_json_formatting_options():
    serialized = await PydanticSerializer(indent=2, ensure_ascii=True).serialize(
        {"message": "こんにちは"}, dict
    )

    assert serialized.decode("utf-8") == (
        '{\n  "message": "\\u3053\\u3093\\u306b\\u3061\\u306f"\n}'
    )


async def test_serialize_accepts_string_indent():
    serialized = await PydanticSerializer(indent="\t").serialize(
        {"message": "hello"}, dict
    )

    assert serialized.decode("utf-8") == '{\n\t"message": "hello"\n}'


async def test_serialize_non_serializable_raises_custom_error(serializer: Serializer):
    with pytest.raises(SerializationError, match="could not be serialized"):
        await serializer.serialize(object(), object)


def test_canonical_opaque_arg_raises_by_default(canonicalizer: ArgsCanonicalizer):
    from glyff.exceptions import ArgumentCanonicalizationError

    class PlainA:
        pass

    def func_with_obj(a: object):
        pass

    sig = inspect.signature(func_with_obj)

    with pytest.raises(ArgumentCanonicalizationError):
        canonicalizer.canonicalize_args(func_with_obj, sig, (PlainA(),), {})


def test_canonical_opaque_arg_by_class_with_qualname_policy():
    from glyff.serialization import QualnameOpaque

    class PlainA:
        pass

    class PlainB:
        pass

    def func_with_obj(a: object):
        pass

    sig = inspect.signature(func_with_obj)

    canonicalizer = PydanticArgsCanonicalizer(opaque_policy=QualnameOpaque())
    first = canonicalizer.canonicalize_args(func_with_obj, sig, (PlainA(),), {})
    second = canonicalizer.canonicalize_args(func_with_obj, sig, (PlainA(),), {})
    different = canonicalizer.canonicalize_args(func_with_obj, sig, (PlainB(),), {})

    # With the opt-in qualname policy, opaque values are identified by their class
    assert first == second
    assert first != different


def test_canonical_nested_dataclass_type_and_callable_values(
    canonicalizer: ArgsCanonicalizer,
):
    @dataclasses.dataclass(frozen=True)
    class Container:
        data: MyDataClass
        cls: type
        callback: object

    def func(container: Container):
        pass

    sig = inspect.signature(func)
    first = canonicalizer.canonicalize_args(
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
    second = canonicalizer.canonicalize_args(
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


def test_canonical_callable_values_by_qualified_name(canonicalizer: ArgsCanonicalizer):
    def func(callback):
        pass

    sig = inspect.signature(func)
    first = canonicalizer.canonicalize_args(func, sig, (helper_func,), {})
    second = canonicalizer.canonicalize_args(func, sig, (helper_func,), {})
    different = canonicalizer.canonicalize_args(func, sig, (another_helper_func,), {})

    assert first == second
    assert first != different


def test_method_identity_differs_for_different_pydantic_instances(
    canonicalizer: ArgsCanonicalizer,
):
    inst1 = MyModel(x=1, y="a")
    inst2 = MyModel(x=2, y="a")
    func = MyModel.method
    sig = inspect.signature(func)

    first = canonicalizer.canonicalize_args(func, sig, (inst1, 10), {})
    second = canonicalizer.canonicalize_args(func, sig, (inst2, 10), {})

    assert first != second


def test_method_identity_is_same_for_identical_pydantic_instances(
    canonicalizer: ArgsCanonicalizer,
):
    inst1 = MyModel(x=1, y="a")
    inst2 = MyModel(x=1, y="a")
    func = MyModel.method
    sig = inspect.signature(func)

    first = canonicalizer.canonicalize_args(func, sig, (inst1, 10), {})
    second = canonicalizer.canonicalize_args(func, sig, (inst2, 10), {})

    assert first == second


def test_canonical_ignores_compare_false_dataclass_fields(
    canonicalizer: ArgsCanonicalizer,
):
    @dataclasses.dataclass
    class AgentWithDep:
        name: str
        counter: int = dataclasses.field(compare=False, default=0)

        def run(self, query: str):
            pass

    func = AgentWithDep.run
    sig = inspect.signature(func)

    first = canonicalizer.canonicalize_args(
        func, sig, (AgentWithDep("a", counter=1), "q"), {}
    )
    second = canonicalizer.canonicalize_args(
        func, sig, (AgentWithDep("a", counter=99), "q"), {}
    )
    other = canonicalizer.canonicalize_args(
        func, sig, (AgentWithDep("b", counter=1), "q"), {}
    )

    assert first == second
    assert first != other


def _agent_model_with_opaque_member():
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

    return Agent, Tool


def test_canonical_model_with_opaque_member_raises_by_default(
    canonicalizer: ArgsCanonicalizer,
):
    """A model holding an opaque member is rejected by the default policy."""
    from glyff.exceptions import ArgumentCanonicalizationError

    Agent, Tool = _agent_model_with_opaque_member()
    func = Agent.run
    sig = inspect.signature(func)
    a1 = Agent(name="researcher", tool=Tool(1))

    with pytest.raises(ArgumentCanonicalizationError):
        canonicalizer.canonicalize_args(func, sig, (a1, "hi"), {})


def test_canonical_model_with_opaque_member_by_class_with_qualname_policy():
    """With the qualname policy, an opaque member folds to its class name.

    The model's serializable state differentiates calls while the opaque member is
    identified by its class instead of raising.
    """
    from glyff.serialization import QualnameOpaque

    Agent, Tool = _agent_model_with_opaque_member()
    func = Agent.run
    sig = inspect.signature(func)

    a1 = Agent(name="researcher", tool=Tool(1))
    a2 = Agent(name="researcher", tool=Tool(2))
    a3 = Agent(name="writer", tool=Tool(1))

    canonicalizer = PydanticArgsCanonicalizer(opaque_policy=QualnameOpaque())
    first = canonicalizer.canonicalize_args(func, sig, (a1, "hi"), {})
    second = canonicalizer.canonicalize_args(func, sig, (a2, "hi"), {})
    other = canonicalizer.canonicalize_args(func, sig, (a3, "hi"), {})

    # Opaque tool identified by class (first == second), serializable state differentiates.
    assert first == second
    assert first != other


def test_model_set_field_is_sorted_for_a_stable_canonical_form(
    canonicalizer: ArgsCanonicalizer,
):
    class M(BaseModel):
        tags: set

    def f(a: object):
        pass

    canonical = canonicalizer.canonicalize_args(
        f, inspect.signature(f), (M(tags={"gamma", "alpha", "beta"}),), {}
    )
    assert canonical == {"a": {"tags": ["alpha", "beta", "gamma"]}}


def test_scalars_pydantic_knows_are_represented_by_value(
    canonicalizer: ArgsCanonicalizer,
):
    class M(BaseModel):
        at: datetime.datetime
        ref: uuid.UUID

    def f(a: object):
        pass

    canonical = canonicalizer.canonicalize_args(
        f,
        inspect.signature(f),
        (M(at=datetime.datetime(2024, 1, 1), ref=uuid.UUID(int=0)),),
        {},
    )
    assert canonical == {
        "a": {
            "at": {"__glyff_opaque__": "2024-01-01T00:00:00"},
            "ref": {"__glyff_opaque__": "00000000-0000-0000-0000-000000000000"},
        }
    }


def test_non_scalars_are_left_to_the_opaque_policy(canonicalizer: ArgsCanonicalizer):
    # pydantic would happily walk an iterable, which would both bypass the shared
    # key checks and consume a generator the engraved call has not run yet.
    def gen():
        yield {1: "integer", "1": "string"}

    def f(a: object):
        pass

    values = gen()
    with pytest.raises(ArgumentCanonicalizationError):
        canonicalizer.canonicalize_args(f, inspect.signature(f), (values,), {})
    assert len(list(values)) == 1


def test_model_mapping_keys_that_collide_are_rejected(
    canonicalizer: ArgsCanonicalizer,
):
    # pydantic's own encoder stringifies mapping keys, which would collapse 1 and
    # "1" into one entry before the shared walk could object.
    class M(BaseModel):
        data: dict[Any, str]

    def f(a: object):
        pass

    with pytest.raises(ArgumentCanonicalizationError, match="canonicalize to"):
        canonicalizer.canonicalize_args(
            f, inspect.signature(f), (M(data={1: "integer", "1": "string"}),), {}
        )


def test_model_set_field_is_content_based(canonicalizer: ArgsCanonicalizer):
    class M(BaseModel):
        tags: set

    def f(a: object):
        pass

    sig = inspect.signature(f)
    first = canonicalizer.canonicalize_args(f, sig, (M(tags={1, 2, 3}),), {})
    second = canonicalizer.canonicalize_args(f, sig, (M(tags={3, 2, 1}),), {})
    other = canonicalizer.canonicalize_args(f, sig, (M(tags={4, 5, 6}),), {})

    assert first == second
    assert first != other
