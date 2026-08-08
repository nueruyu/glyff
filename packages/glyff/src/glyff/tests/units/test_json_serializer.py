import dataclasses
import inspect

import pytest

from glyff.exceptions import SerializationError, ArgumentCanonicalizationError
from glyff.serialization import (
    JsonArgumentCanonicalizer,
    JsonSerializer,
    OpaquePolicy,
    OpaqueByTypeQualname,
)


def sample_func(a: int, b: str = "default"):
    pass


def bound(func, *args, **kwargs) -> dict:
    """The name-to-value mapping one call binds to.

    Scaffolding only: what binding itself guarantees is
    ``FunctionDefinition.bind``'s to prove.
    """
    arguments = inspect.signature(func).bind(*args, **kwargs)
    arguments.apply_defaults()
    return dict(arguments.arguments)


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
def argument_canonicalizer() -> JsonArgumentCanonicalizer:
    return JsonArgumentCanonicalizer()


def test_canonicalize_args_different_values_differ(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    first = argument_canonicalizer.canonicalize(bound(sample_func, 1))
    second = argument_canonicalizer.canonicalize(bound(sample_func, 2))
    assert first != second


def test_canonicalize_args_includes_var_positional_and_keyword(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    def func(a, *args, **kwargs):
        pass

    # *args and **kwargs must affect identity, otherwise distinct calls collide.
    assert argument_canonicalizer.canonicalize(
        bound(func, 1, 2)
    ) != argument_canonicalizer.canonicalize(bound(func, 1, 3))
    assert argument_canonicalizer.canonicalize(
        bound(func, 1, x=2)
    ) != argument_canonicalizer.canonicalize(bound(func, 1, x=3))
    assert argument_canonicalizer.canonicalize(
        bound(func, 1, 2, x=3)
    ) == argument_canonicalizer.canonicalize(bound(func, 1, 2, x=3))


def test_canonicalize_args_is_deterministic(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    first = argument_canonicalizer.canonicalize(bound(sample_func, 42, b="hello"))
    second = argument_canonicalizer.canonicalize(bound(sample_func, 42, b="hello"))
    assert first == second


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


def test_canonical_opaque_arg_raises_by_default(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    def func_with_obj(a: object):
        pass

    # By default an opaque value (no value representation) is rejected rather than
    with pytest.raises(ArgumentCanonicalizationError):
        argument_canonicalizer.canonicalize(bound(func_with_obj, MyPlainClass("id1")))


def test_canonical_opaque_arg_by_class_with_qualname_policy():
    argument_canonicalizer = JsonArgumentCanonicalizer(
        opaque_policy=OpaqueByTypeQualname()
    )

    def func_with_obj(a: object):
        pass

    first = argument_canonicalizer.canonicalize(
        bound(func_with_obj, MyPlainClass("id1"))
    )
    second = argument_canonicalizer.canonicalize(
        bound(func_with_obj, MyPlainClass("id2"))
    )
    different = argument_canonicalizer.canonicalize(bound(func_with_obj, object()))

    # With the opt-in qualname policy, opaque values are identified by their class:
    assert first == second
    assert first != different


def test_canonical_nested_dataclass_and_type_values(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    @dataclasses.dataclass(frozen=True)
    class Container:
        data: MyDataClass
        cls: type

    def func(container: Container):
        pass

    first = argument_canonicalizer.canonicalize(
        bound(func, Container(data=MyDataClass(id="id1", value=100), cls=AnotherClass))
    )
    second = argument_canonicalizer.canonicalize(
        bound(func, Container(data=MyDataClass(id="id1", value=100), cls=AnotherClass))
    )

    assert first == second


def test_canonical_callable_values_by_qualified_name(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    def func(callback):
        pass

    first = argument_canonicalizer.canonicalize(bound(func, helper_func))
    second = argument_canonicalizer.canonicalize(bound(func, helper_func))
    different = argument_canonicalizer.canonicalize(bound(func, another_helper_func))

    assert first == second
    assert first != different


def test_canonical_partial_by_components(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    import functools

    def func(callback):
        pass

    def base(a, b):
        return a + b

    # partials hash by their func/args/keywords, not all collapsing to "functools.partial".
    first = argument_canonicalizer.canonicalize(bound(func, functools.partial(base, 1)))
    h1_again = argument_canonicalizer.canonicalize(
        bound(func, functools.partial(base, 1))
    )
    diff_arg = argument_canonicalizer.canonicalize(
        bound(func, functools.partial(base, 2))
    )
    diff_kw = argument_canonicalizer.canonicalize(
        bound(func, functools.partial(base, 1, b=9))
    )
    diff_func = argument_canonicalizer.canonicalize(
        bound(func, functools.partial(helper_func))
    )

    assert first == h1_again
    assert first != diff_arg
    assert first != diff_kw
    assert first != diff_func


def test_method_hash_differs_for_different_dataclass_instances(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    inst1 = MyDataClass(id="id1", value=100)
    inst2 = MyDataClass(id="id2", value=100)
    func = MyDataClass.method

    first = argument_canonicalizer.canonicalize(bound(func, inst1, 10))
    second = argument_canonicalizer.canonicalize(bound(func, inst2, 10))

    assert first != second


def test_method_hash_is_same_for_identical_dataclass_instances(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    inst1 = MyDataClass(id="id1", value=100)
    inst2 = MyDataClass(id="id1", value=100)
    func = MyDataClass.method

    first = argument_canonicalizer.canonicalize(bound(func, inst1, 10))
    second = argument_canonicalizer.canonicalize(bound(func, inst2, 10))

    assert first == second


def test_class_method_hash_is_stable(argument_canonicalizer: JsonArgumentCanonicalizer):
    func = AnotherClass.class_method.__func__

    first = argument_canonicalizer.canonicalize(bound(func, AnotherClass, 10))
    second = argument_canonicalizer.canonicalize(bound(func, AnotherClass, 10))

    assert first == second


def test_canonical_plain_self_raises_by_default(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    func = MyPlainClass.method

    # A plain (non-dataclass) `self` has no value representation, so by default it is
    # rejected instead of being collapsed to its class name.
    with pytest.raises(ArgumentCanonicalizationError):
        argument_canonicalizer.canonicalize(bound(func, MyPlainClass("id1"), 10))


def test_canonical_plain_self_by_class_with_qualname_policy():
    argument_canonicalizer = JsonArgumentCanonicalizer(
        opaque_policy=OpaqueByTypeQualname()
    )
    func = MyPlainClass.method

    # Opt in to qualname hashing to treat the receiver as a stateless service: calls on
    # different instances of the same class collapse while arguments still differentiate.
    first = argument_canonicalizer.canonicalize(bound(func, MyPlainClass("id1"), 10))
    second = argument_canonicalizer.canonicalize(bound(func, MyPlainClass("id2"), 10))
    other = argument_canonicalizer.canonicalize(bound(func, MyPlainClass("id1"), 20))

    assert first == second
    assert first != other


def test_canonical_dataclass_with_nested_plain_service():
    """A dataclass (state matters) holding plain, non-deepcopyable services.

    Under the default policy a nested opaque member is rejected; with the qualname
    policy it is identified by its class while the dataclass state differentiates calls,
    even when the member holds objects that cannot be deep-copied.
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

    a1 = Agent("researcher", [Tool(threading.Lock())])
    a2 = Agent("researcher", [Tool(threading.Lock())])
    a3 = Agent("writer", [Tool(threading.Lock())])

    with pytest.raises(ArgumentCanonicalizationError):
        JsonArgumentCanonicalizer().canonicalize(bound(func, a1, "hi"))

    argument_canonicalizer = JsonArgumentCanonicalizer(
        opaque_policy=OpaqueByTypeQualname()
    )
    first = argument_canonicalizer.canonicalize(bound(func, a1, "hi"))
    second = argument_canonicalizer.canonicalize(bound(func, a2, "hi"))
    other = argument_canonicalizer.canonicalize(bound(func, a3, "hi"))

    assert first == second
    assert first != other


def test_custom_opaque_policy_receives_the_value():
    class Marker:
        pass

    seen: list = []

    class RecordingPolicy(OpaquePolicy):
        def represent(self, value):
            seen.append(value)
            return "recorded"

    argument_canonicalizer = JsonArgumentCanonicalizer(opaque_policy=RecordingPolicy())

    def func(a: object):
        pass

    marker = Marker()
    argument_canonicalizer.canonicalize(bound(func, marker))

    assert seen == [marker]


def test_opaque_value_does_not_collide_with_native_representation():
    """An opaque value must not equal a native value sharing the policy's output.

    OpaqueByTypeQualname renders MyPlainClass as its qualified name; a literal string equal
    to that qualname must still hash differently, since the framework tags opaque output.
    """
    argument_canonicalizer = JsonArgumentCanonicalizer(
        opaque_policy=OpaqueByTypeQualname()
    )

    def func(a: object):
        pass

    qualname = f"{MyPlainClass.__module__}.{MyPlainClass.__qualname__}"
    opaque = argument_canonicalizer.canonicalize(bound(func, MyPlainClass("id1")))
    literal = argument_canonicalizer.canonicalize(bound(func, qualname))

    assert opaque != literal


def test_custom_opaque_policy_result_differentiates_hash():
    """The policy's return value flows into the hash: distinct results differ."""

    class Tok:
        def __init__(self, token):
            self.token = token

    class ByToken(OpaquePolicy):
        def represent(self, value):
            return value.token

    argument_canonicalizer = JsonArgumentCanonicalizer(opaque_policy=ByToken())

    def func(a: object):
        pass

    a = argument_canonicalizer.canonicalize(bound(func, Tok("a")))
    a_again = argument_canonicalizer.canonicalize(bound(func, Tok("a")))
    b = argument_canonicalizer.canonicalize(bound(func, Tok("b")))

    assert a == a_again
    assert a != b


def test_opaque_policy_applies_to_nested_dataclass_member():
    """The injected policy governs opaque values nested inside a dataclass."""

    class Svc:
        pass

    @dataclasses.dataclass
    class Holder:
        svc: object

    def func(a: object):
        pass

    with pytest.raises(ArgumentCanonicalizationError):
        JsonArgumentCanonicalizer().canonicalize(bound(func, Holder(Svc())))

    argument_canonicalizer = JsonArgumentCanonicalizer(
        opaque_policy=OpaqueByTypeQualname()
    )
    first = argument_canonicalizer.canonicalize(bound(func, Holder(Svc())))
    second = argument_canonicalizer.canonicalize(bound(func, Holder(Svc())))
    assert first == second


def test_opaque_policy_applies_to_set_members():
    """The injected policy governs opaque values inside a set (via the sort-key path)."""

    class Svc:
        pass

    def func(a: set):
        pass

    values = {Svc(), Svc()}

    with pytest.raises(ArgumentCanonicalizationError):
        JsonArgumentCanonicalizer().canonicalize(bound(func, values))

    argument_canonicalizer = JsonArgumentCanonicalizer(
        opaque_policy=OpaqueByTypeQualname()
    )
    assert argument_canonicalizer.canonicalize(bound(func, values)) is not None


def test_falsy_custom_opaque_policy_is_respected():
    """A custom policy that is falsy must not be silently replaced by the default."""

    class FalsyPolicy(OpaquePolicy):
        def __bool__(self):
            return False

        def represent(self, value):
            return "from-falsy"

    class Svc:
        pass

    argument_canonicalizer = JsonArgumentCanonicalizer(opaque_policy=FalsyPolicy())

    def func(a: object):
        pass

    # Would raise if the falsy policy were dropped in favour of RejectOpaque.
    assert argument_canonicalizer.canonicalize(bound(func, Svc())) is not None


def test_canonical_set_is_by_content_and_order_independent(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    def func(a: set):
        pass

    # Value types json doesn't encode natively (set/frozenset) are hashed by content,
    # independent of insertion order, rather than colliding on "builtins.set".
    h_ab = argument_canonicalizer.canonicalize(bound(func, {1, 2, 3}))
    h_ba = argument_canonicalizer.canonicalize(bound(func, {3, 2, 1}))
    h_cd = argument_canonicalizer.canonicalize(bound(func, {4, 5, 6}))

    assert h_ab == h_ba
    assert h_ab != h_cd


def test_canonical_frozenset_matches_equivalent_set(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    def func(a: object):
        pass

    h_set = argument_canonicalizer.canonicalize(bound(func, {1, 2}))
    h_frozen = argument_canonicalizer.canonicalize(bound(func, frozenset({2, 1})))

    # Both reduce to the same sorted content representation.
    assert h_set == h_frozen


def test_canonical_bytes_is_by_content(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    def func(a: bytes):
        pass

    first = argument_canonicalizer.canonicalize(bound(func, b"abc"))
    second = argument_canonicalizer.canonicalize(bound(func, b"abc"))
    other = argument_canonicalizer.canonicalize(bound(func, b"xyz"))

    assert first == second
    assert first != other


@dataclasses.dataclass
class AgentWithDep:
    name: str
    # A non-identity member (an injected dependency / mutable counter) that should not
    # influence the hash; identifying state lives in `name`.
    counter: int = dataclasses.field(compare=False, default=0)

    def run(self, query: str):
        pass


def test_canonical_ignores_compare_false_dataclass_fields(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    func = AgentWithDep.run

    # `counter` is field(compare=False), so it is excluded from the hash; only `name`
    # and the call arguments differentiate.
    first = argument_canonicalizer.canonicalize(
        bound(func, AgentWithDep("a", counter=1), "q")
    )
    second = argument_canonicalizer.canonicalize(
        bound(func, AgentWithDep("a", counter=99), "q")
    )
    other = argument_canonicalizer.canonicalize(
        bound(func, AgentWithDep("b", counter=1), "q")
    )

    assert first == second
    assert first != other


async def test_serialize_includes_compare_false_dataclass_fields(
    serializer: JsonSerializer,
):
    # Serialization must round-trip real data, so excluded-from-hash fields are still
    # serialized.
    data = await serializer.serialize(AgentWithDep("a", counter=7), AgentWithDep)
    assert b"counter" in data
    assert b"7" in data


def test_regular_function_with_self_parameter_is_hashed_normally(
    argument_canonicalizer: JsonArgumentCanonicalizer,
):
    def regular_func(self: str, x: int):
        pass

    result = argument_canonicalizer.canonicalize(
        bound(regular_func, "a serializable string", 10)
    )
    assert result is not None
