"""What the JSON implementations add on top of the contracts they satisfy.

The contracts run in `test_json_serialization_contract.py`, and which Python
value becomes which canonical form is `to_canonical`'s to promise — see
`test_serialization_helpers.py`.
"""

import dataclasses

import pytest

from glyff.exceptions import ArgumentCanonicalizationError, SerializationError
from glyff.serialization import (
    JsonArgumentCanonicalizer,
    JsonSerializer,
    FallbackByTypeQualname,
    CanonicalFallbackRepresenter,
)


class MyPlainClass:
    def __init__(self, id_val: str):
        self.id = id_val


@pytest.fixture
def serializer() -> JsonSerializer:
    return JsonSerializer()


@pytest.fixture
def argument_canonicalizer() -> JsonArgumentCanonicalizer:
    return JsonArgumentCanonicalizer()


# -- The shipped fallback representer ----------------------------------------


def test_the_qualname_representer_identifies_a_value_by_its_class():
    canonicalizer = JsonArgumentCanonicalizer(
        fallback_representer=FallbackByTypeQualname()
    )

    first = canonicalizer.canonicalize({"a": MyPlainClass("id1")})
    second = canonicalizer.canonicalize({"a": MyPlainClass("id2")})
    different = canonicalizer.canonicalize({"a": object()})

    assert first == second
    assert first != different


def test_a_qualname_rendered_value_does_not_collide_with_that_literal_string():
    # The representer returns MyPlainClass's qualified name, so a string equal to
    # that name would key the same call unless fallback output carried a tag.
    canonicalizer = JsonArgumentCanonicalizer(
        fallback_representer=FallbackByTypeQualname()
    )
    qualname = f"{MyPlainClass.__module__}.{MyPlainClass.__qualname__}"

    assert canonicalizer.canonicalize(
        {"a": MyPlainClass("id1")}
    ) != canonicalizer.canonicalize({"a": qualname})


def test_the_qualname_fallback_reaches_an_unsupported_dataclass_member():
    # The realistic shape: an agent whose identity is its state, holding
    # dependencies that cannot even be deep-copied.
    import threading

    class Tool:
        def __init__(self, lock):
            self.lock = lock

    @dataclasses.dataclass
    class Agent:
        name: str
        tools: list

    a1 = Agent("researcher", [Tool(threading.Lock())])
    a2 = Agent("researcher", [Tool(threading.Lock())])
    a3 = Agent("writer", [Tool(threading.Lock())])

    with pytest.raises(ArgumentCanonicalizationError):
        JsonArgumentCanonicalizer().canonicalize({"self": a1, "query": "hi"})

    canonicalizer = JsonArgumentCanonicalizer(
        fallback_representer=FallbackByTypeQualname()
    )
    assert canonicalizer.canonicalize(
        {"self": a1, "query": "hi"}
    ) == canonicalizer.canonicalize({"self": a2, "query": "hi"})
    assert canonicalizer.canonicalize(
        {"self": a1, "query": "hi"}
    ) != canonicalizer.canonicalize({"self": a3, "query": "hi"})


def test_a_fallback_governs_unsupported_members_of_a_set():
    # A set is ordered by a sort key before it becomes a list, which is a second
    # path a value can take through the walk.
    class Svc:
        pass

    values = {Svc(), Svc()}

    with pytest.raises(ArgumentCanonicalizationError):
        JsonArgumentCanonicalizer().canonicalize({"a": values})

    canonicalizer = JsonArgumentCanonicalizer(
        fallback_representer=FallbackByTypeQualname()
    )
    assert canonicalizer.canonicalize({"a": values}) is not None


# -- Holding the fallback representer it was given ---------------------------


def test_the_representer_is_handed_the_value_itself():
    class Marker:
        pass

    seen: list = []

    class RecordingRepresenter(CanonicalFallbackRepresenter):
        def represent(self, value):
            seen.append(value)
            return "recorded"

    marker = Marker()
    JsonArgumentCanonicalizer(fallback_representer=RecordingRepresenter()).canonicalize(
        {"a": marker}
    )

    assert seen == [marker]


def test_what_a_representer_returns_is_what_reaches_the_form():
    # Being handed the value is not enough: a canonicalizer that called the
    # representer and then rendered the value its own way would still tell two
    # instances apart, so only a representer that disagrees with that rendering
    # catches it.
    class Svc:
        def __init__(self, name: str):
            self.name = name

    class ByName(CanonicalFallbackRepresenter):
        def represent(self, value):
            return value.name

    canonicalizer = JsonArgumentCanonicalizer(fallback_representer=ByName())

    assert canonicalizer.canonicalize({"a": Svc("one")}) != canonicalizer.canonicalize(
        {"a": Svc("two")}
    )


def test_a_falsy_representer_is_not_mistaken_for_no_representer():
    class FalsyRepresenter(CanonicalFallbackRepresenter):
        def __bool__(self):
            return False

        def represent(self, value):
            return "from-falsy"

    class Svc:
        pass

    canonicalizer = JsonArgumentCanonicalizer(fallback_representer=FalsyRepresenter())

    # Would raise if the falsy representer were mistaken for no representer.
    assert canonicalizer.canonicalize({"a": Svc()}) is not None


# -- Serializing is not canonicalizing ---------------------------------------


@dataclasses.dataclass
class AgentWithDep:
    name: str
    counter: int = dataclasses.field(compare=False, default=0)


async def test_serializing_keeps_a_field_canonicalizing_drops(
    serializer: JsonSerializer,
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    # The two disagree on purpose: identity ignores a `compare=False` field, but
    # a recorded result has to carry the real data back.
    assert argument_canonicalizer.canonicalize(
        {"a": AgentWithDep("a", counter=1)}
    ) == argument_canonicalizer.canonicalize({"a": AgentWithDep("a", counter=99)})

    data = await serializer.serialize(AgentWithDep("a", counter=7), AgentWithDep)
    assert b"counter" in data
    assert b"7" in data


# -- The bytes it writes ------------------------------------------------------


async def test_the_bytes_do_not_depend_on_mapping_order(serializer: JsonSerializer):
    # `stable_json_dumps` sorts keys, and this is what wires the serializer to
    # it: two equal mappings must reach a store as one value.
    assert await serializer.serialize({"b": 2, "a": 1}, dict) == (
        await serializer.serialize({"a": 1, "b": 2}, dict)
    )


async def test_a_value_json_cannot_carry_is_refused(serializer: JsonSerializer):
    with pytest.raises(SerializationError, match="could not be serialized to JSON"):
        await serializer.serialize(object(), object)


async def test_serialized_output_defaults_to_compact_readable_json():
    assert await JsonSerializer().serialize({"message": "こんにちは"}, dict) == (
        '{"message":"こんにちは"}'.encode()
    )


async def test_a_serializer_accepts_json_formatting_options():
    serializer = JsonSerializer(indent=2, ensure_ascii=True)

    assert await serializer.serialize({"message": "こんにちは"}, dict) == (
        b'{\n  "message": "\\u3053\\u3093\\u306b\\u3061\\u306f"\n}'
    )


async def test_a_serializer_accepts_a_string_indent():
    assert await JsonSerializer(indent="\t").serialize({"message": "hello"}, dict) == (
        b'{\n\t"message": "hello"\n}'
    )
