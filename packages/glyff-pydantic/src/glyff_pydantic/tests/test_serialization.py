import dataclasses
import datetime
import enum
import uuid
from typing import Any

import pytest
from glyff.exceptions import SerializationError, ArgumentCanonicalizationError
from glyff import ArgumentCanonicalizer, Serializer
from pydantic import BaseModel

from glyff_pydantic import PydanticArgumentCanonicalizer, PydanticSerializer


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


@pytest.fixture
def serializer() -> Serializer:
    return PydanticSerializer()


@pytest.fixture
def argument_canonicalizer() -> ArgumentCanonicalizer:
    return PydanticArgumentCanonicalizer()


def test_canonicalize_args_different_values_differ(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    first = argument_canonicalizer.canonicalize({"a": 1})
    second = argument_canonicalizer.canonicalize({"a": 2})
    assert first != second


def test_canonicalize_args_is_deterministic(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    first = argument_canonicalizer.canonicalize({"a": 42, "b": "hello"})
    second = argument_canonicalizer.canonicalize({"a": 42, "b": "hello"})
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


def test_canonical_opaque_arg_raises_by_default(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    from glyff.exceptions import ArgumentCanonicalizationError

    class PlainA:
        pass

    with pytest.raises(ArgumentCanonicalizationError):
        argument_canonicalizer.canonicalize({"a": PlainA()})


def test_canonical_opaque_arg_by_class_with_qualname_policy():
    from glyff.serialization import OpaqueByTypeQualname

    class PlainA:
        pass

    class PlainB:
        pass

    argument_canonicalizer = PydanticArgumentCanonicalizer(
        opaque_policy=OpaqueByTypeQualname()
    )
    first = argument_canonicalizer.canonicalize({"a": PlainA()})
    second = argument_canonicalizer.canonicalize({"a": PlainA()})
    different = argument_canonicalizer.canonicalize({"a": PlainB()})

    # With the opt-in qualname policy, opaque values are identified by their class
    assert first == second
    assert first != different


def test_canonical_nested_dataclass_type_and_callable_values(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    @dataclasses.dataclass(frozen=True)
    class Container:
        data: MyDataClass
        cls: type
        callback: object

    first = argument_canonicalizer.canonicalize(
        {
            "container": Container(
                data=MyDataClass(id="id1", value=100),
                cls=MyPlainClass,
                callback=helper_func,
            )
        }
    )
    second = argument_canonicalizer.canonicalize(
        {
            "container": Container(
                data=MyDataClass(id="id1", value=100),
                cls=MyPlainClass,
                callback=helper_func,
            )
        }
    )

    assert first == second


def test_canonical_callable_values_by_qualified_name(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    first = argument_canonicalizer.canonicalize({"callback": helper_func})
    second = argument_canonicalizer.canonicalize({"callback": helper_func})
    different = argument_canonicalizer.canonicalize({"callback": another_helper_func})

    assert first == second
    assert first != different


def test_method_identity_differs_for_different_pydantic_instances(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    inst1 = MyModel(x=1, y="a")
    inst2 = MyModel(x=2, y="a")
    first = argument_canonicalizer.canonicalize({"self": inst1, "x": 10})
    second = argument_canonicalizer.canonicalize({"self": inst2, "x": 10})

    assert first != second


def test_method_identity_is_same_for_identical_pydantic_instances(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    inst1 = MyModel(x=1, y="a")
    inst2 = MyModel(x=1, y="a")
    first = argument_canonicalizer.canonicalize({"self": inst1, "x": 10})
    second = argument_canonicalizer.canonicalize({"self": inst2, "x": 10})

    assert first == second


def test_canonical_ignores_compare_false_dataclass_fields(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    @dataclasses.dataclass
    class AgentWithDep:
        name: str
        counter: int = dataclasses.field(compare=False, default=0)

        def run(self, query: str):
            pass

    first = argument_canonicalizer.canonicalize(
        {"self": AgentWithDep("a", counter=1), "query": "q"}
    )
    second = argument_canonicalizer.canonicalize(
        {"self": AgentWithDep("a", counter=99), "query": "q"}
    )
    other = argument_canonicalizer.canonicalize(
        {"self": AgentWithDep("b", counter=1), "query": "q"}
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
    argument_canonicalizer: ArgumentCanonicalizer,
):
    """A model holding an opaque member is rejected by the default policy."""
    from glyff.exceptions import ArgumentCanonicalizationError

    Agent, Tool = _agent_model_with_opaque_member()
    a1 = Agent(name="researcher", tool=Tool(1))

    with pytest.raises(ArgumentCanonicalizationError):
        argument_canonicalizer.canonicalize({"self": a1, "query": "hi"})


def test_canonical_model_with_opaque_member_by_class_with_qualname_policy():
    """With the qualname policy, an opaque member folds to its class name.

    The model's serializable state differentiates calls while the opaque member is
    identified by its class instead of raising.
    """
    from glyff.serialization import OpaqueByTypeQualname

    Agent, Tool = _agent_model_with_opaque_member()
    a1 = Agent(name="researcher", tool=Tool(1))
    a2 = Agent(name="researcher", tool=Tool(2))
    a3 = Agent(name="writer", tool=Tool(1))

    argument_canonicalizer = PydanticArgumentCanonicalizer(
        opaque_policy=OpaqueByTypeQualname()
    )
    first = argument_canonicalizer.canonicalize({"self": a1, "query": "hi"})
    second = argument_canonicalizer.canonicalize({"self": a2, "query": "hi"})
    other = argument_canonicalizer.canonicalize({"self": a3, "query": "hi"})

    # Opaque tool identified by class (first == second), serializable state differentiates.
    assert first == second
    assert first != other


def test_model_set_field_is_sorted_for_a_stable_canonical_form(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    class M(BaseModel):
        tags: set

    def f(a: object):
        pass

    canonical = argument_canonicalizer.canonicalize(
        {"a": M(tags={"gamma", "alpha", "beta"})}
    )
    assert canonical == {"a": {"tags": ["alpha", "beta", "gamma"]}}


def test_scalars_pydantic_knows_are_represented_by_value(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    class M(BaseModel):
        at: datetime.datetime
        ref: uuid.UUID

    def f(a: object):
        pass

    canonical = argument_canonicalizer.canonicalize(
        {"a": M(at=datetime.datetime(2024, 1, 1), ref=uuid.UUID(int=0))}
    )
    assert canonical == {
        "a": {
            "at": {"__glyff_opaque__": "2024-01-01T00:00:00"},
            "ref": {"__glyff_opaque__": "00000000-0000-0000-0000-000000000000"},
        }
    }


def test_mapping_valued_enums_keep_distinct_identity(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    # An Enum member's value can be a container, so it cannot go to pydantic as a
    # scalar: that would stringify the mapping keys before the shared walk sees them.
    class Colliding(enum.Enum):
        VALUE = {1: "integer", "1": "string"}

    def f(a: object):
        pass

    with pytest.raises(ArgumentCanonicalizationError, match="canonicalize to"):
        argument_canonicalizer.canonicalize({"a": Colliding.VALUE})


def test_scalar_valued_enums_are_represented_by_value(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    class Colour(enum.Enum):
        RED = "red"

    def f(a: object):
        pass

    canonical = argument_canonicalizer.canonicalize({"a": Colour.RED})
    assert canonical == {"a": {"__glyff_opaque__": "red"}}


def test_non_scalars_are_left_to_the_opaque_policy(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    # pydantic would happily walk an iterable, which would both bypass the shared
    # key checks and consume a generator the engraved call has not run yet.
    def gen():
        yield {1: "integer", "1": "string"}

    def f(a: object):
        pass

    values = gen()
    with pytest.raises(ArgumentCanonicalizationError):
        argument_canonicalizer.canonicalize({"a": values})
    assert len(list(values)) == 1


def test_model_mapping_keys_that_collide_are_rejected(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    # pydantic's own encoder stringifies mapping keys, which would collapse 1 and
    # "1" into one entry before the shared walk could object.
    class M(BaseModel):
        data: dict[Any, str]

    def f(a: object):
        pass

    with pytest.raises(ArgumentCanonicalizationError, match="canonicalize to"):
        argument_canonicalizer.canonicalize(
            {"a": M(data={1: "integer", "1": "string"})}
        )


def test_model_set_field_is_content_based(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    class M(BaseModel):
        tags: set

    def f(a: object):
        pass

    first = argument_canonicalizer.canonicalize({"a": M(tags={1, 2, 3})})
    second = argument_canonicalizer.canonicalize({"a": M(tags={3, 2, 1})})
    other = argument_canonicalizer.canonicalize({"a": M(tags={4, 5, 6})})

    assert first == second
    assert first != other
