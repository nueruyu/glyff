"""What Pydantic changes about canonicalizing and serializing.

The contracts run in `test_serialization_contract.py`; everything inherited from
the JSON implementations is proved beside those, in `glyff`.
"""

import dataclasses
import datetime
import enum
import uuid
from typing import Any

import pytest
from glyff import (
    ArgumentCanonicalizer,
    CanonicalArguments,
    CanonicalFallback,
    Serializer,
)
from glyff.exceptions import ArgumentCanonicalizationError, SerializationError
from glyff.serialization import FallbackByTypeQualname
from pydantic import BaseModel, ConfigDict

from glyff_pydantic import PydanticArgumentCanonicalizer, PydanticSerializer


class MyModel(BaseModel):
    x: int
    y: str


@pytest.fixture
def serializer() -> Serializer:
    return PydanticSerializer()


@pytest.fixture
def argument_canonicalizer() -> ArgumentCanonicalizer:
    return PydanticArgumentCanonicalizer()


# -- Models as serialized values ---------------------------------------------


async def test_a_model_survives_a_round_trip(serializer: Serializer):
    model = MyModel(x=42, y="hello")

    assert (
        await serializer.deserialize(
            await serializer.serialize(model, MyModel), MyModel
        )
        == model
    )


async def test_a_list_of_models_survives_a_round_trip(serializer: Serializer):
    models = [MyModel(x=i, y=str(i)) for i in range(3)]

    assert (
        await serializer.deserialize(
            await serializer.serialize(models, list[MyModel]), list[MyModel]
        )
        == models
    )


async def test_a_value_pydantic_cannot_carry_is_refused(serializer: Serializer):
    # `PydanticSerializer` overrides `serialize` and wraps the failure itself,
    # so this is its own error path rather than the JSON serializer's.
    with pytest.raises(SerializationError, match="could not be serialized"):
        await serializer.serialize(object(), object)


async def test_the_encoding_options_reach_the_output():
    # `serialize` is overridden here but the encoding is the parent's, so this is
    # the wiring: a rewrite that stopped going through `_encode` would keep the
    # JSON serializer's own tests green.
    serializer = PydanticSerializer(indent=2, ensure_ascii=True)

    assert await serializer.serialize({"message": "こんにちは"}, dict) == (
        b'{\n  "message": "\\u3053\\u3093\\u306b\\u3061\\u306f"\n}'
    )


async def test_key_order_does_not_reach_the_output(serializer: Serializer):
    # The same wiring as above on the axis the options cannot reveal: a rewrite
    # that formatted its own JSON could keep `indent` and `ensure_ascii` and
    # still lose the sorted keys the parent asks for.
    assert await serializer.serialize({"b": 2, "a": 1}, dict) == (
        await serializer.serialize({"a": 1, "b": 2}, dict)
    )


# -- Models as identity -------------------------------------------------------


def test_a_models_state_is_its_identity(argument_canonicalizer: ArgumentCanonicalizer):
    assert argument_canonicalizer.canonicalize(
        {"self": MyModel(x=1, y="a")}
    ) == argument_canonicalizer.canonicalize({"self": MyModel(x=1, y="a")})
    assert argument_canonicalizer.canonicalize(
        {"self": MyModel(x=1, y="a")}
    ) != argument_canonicalizer.canonicalize({"self": MyModel(x=2, y="a")})


def _agent_model_with_unsupported_member():
    class Tool:
        def __init__(self, n):
            self.n = n

    class Agent(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        name: str
        tool: Tool

    return Agent, Tool


def test_a_model_holding_an_unsupported_member_is_refused_by_default(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    Agent, Tool = _agent_model_with_unsupported_member()

    with pytest.raises(ArgumentCanonicalizationError):
        argument_canonicalizer.canonicalize(
            {"self": Agent(name="researcher", tool=Tool(1))}
        )


def test_a_fallback_representer_reaches_an_unsupported_model_member():
    # The shape an agent object usually has: identity is the model's state,
    # carried alongside a dependency that has none.
    Agent, Tool = _agent_model_with_unsupported_member()
    canonicalizer = PydanticArgumentCanonicalizer(
        fallback_representer=FallbackByTypeQualname()
    )

    assert canonicalizer.canonicalize(
        {"self": Agent(name="researcher", tool=Tool(1))}
    ) == canonicalizer.canonicalize({"self": Agent(name="researcher", tool=Tool(2))})
    assert canonicalizer.canonicalize(
        {"self": Agent(name="researcher", tool=Tool(1))}
    ) != canonicalizer.canonicalize({"self": Agent(name="writer", tool=Tool(1))})


# -- What Pydantic can represent that plain JSON cannot ----------------------


def test_a_scalar_pydantic_knows_is_represented_by_value(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    class M(BaseModel):
        at: datetime.datetime
        ref: uuid.UUID

    assert argument_canonicalizer.canonicalize(
        {"a": M(at=datetime.datetime(2024, 1, 1), ref=uuid.UUID(int=0))}
    ) == CanonicalArguments(
        {
            "a": {
                "at": CanonicalFallback("2024-01-01T00:00:00"),
                "ref": CanonicalFallback("00000000-0000-0000-0000-000000000000"),
            }
        }
    )


def test_a_scalar_valued_enum_is_represented_by_value(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    class Colour(enum.Enum):
        RED = "red"

    assert argument_canonicalizer.canonicalize({"a": Colour.RED}) == CanonicalArguments(
        {"a": CanonicalFallback("red")}
    )


def test_an_enum_preserves_the_representation_of_its_non_json_value(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    class StartedAt(enum.Enum):
        MIDNIGHT = datetime.datetime(2024, 1, 1)

    canonical = argument_canonicalizer.canonicalize({"a": StartedAt.MIDNIGHT})
    assert canonical == CanonicalArguments(
        {"a": CanonicalFallback(CanonicalFallback("2024-01-01T00:00:00"))}
    )
    assert argument_canonicalizer.canonicalize(canonical.decode()) == canonical


# -- Keeping Pydantic's encoder out of the walk ------------------------------


def test_a_models_set_field_keeps_the_shared_ordering(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    class M(BaseModel):
        tags: set

    assert argument_canonicalizer.canonicalize(
        {"a": M(tags={"gamma", "alpha", "beta"})}
    ) == CanonicalArguments({"a": {"tags": ["alpha", "beta", "gamma"]}})


def test_a_models_colliding_mapping_keys_are_still_refused(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    # Pydantic's own encoder stringifies mapping keys, which would collapse 1 and
    # "1" into one entry before the shared walk could object.
    class M(BaseModel):
        data: dict[Any, str]

    with pytest.raises(ArgumentCanonicalizationError, match="canonicalize to"):
        argument_canonicalizer.canonicalize(
            {"a": M(data={1: "integer", "1": "string"})}
        )


def test_a_mapping_valued_enum_is_not_handed_over_as_a_scalar(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    # An Enum member's value can be a container, so it cannot go to Pydantic as a
    # scalar: that would stringify the mapping keys before the walk sees them.
    class Colliding(enum.Enum):
        VALUE = {1: "integer", "1": "string"}

    with pytest.raises(ArgumentCanonicalizationError, match="canonicalize to"):
        argument_canonicalizer.canonicalize({"a": Colliding.VALUE})


def test_a_generator_is_left_to_the_fallback_representer(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    # Pydantic would happily walk an iterable, which would both bypass the shared
    # key checks and consume a generator the engraved call has not run yet.
    def gen():
        yield {1: "integer", "1": "string"}

    values = gen()
    with pytest.raises(ArgumentCanonicalizationError):
        argument_canonicalizer.canonicalize({"a": values})
    assert len(list(values)) == 1


def test_a_dataclass_still_reaches_the_shared_walk(
    argument_canonicalizer: ArgumentCanonicalizer,
):
    # Pydantic dumps dataclasses too, so this checks the override defers rather
    # than taking a value the shared rules should have handled.
    @dataclasses.dataclass
    class AgentWithDep:
        name: str
        counter: int = dataclasses.field(compare=False, default=0)

    assert argument_canonicalizer.canonicalize(
        {"self": AgentWithDep("a", counter=1)}
    ) == argument_canonicalizer.canonicalize({"self": AgentWithDep("a", counter=99)})
