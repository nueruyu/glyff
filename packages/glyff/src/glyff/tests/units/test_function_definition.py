"""What glyff reads off an engraved function, and what it refuses to read."""

import pytest
from glyff._function import FunctionDefinition
from glyff.exceptions import MissingTypeHintError, TypeHintResolutionError


async def greet(name: str, greeting: str = "Hello") -> str:
    return f"{greeting}, {name}!"


def test_a_definition_carries_the_name_and_return_type():
    definition = FunctionDefinition.from_callable(greet)

    assert definition.name.value == "greet"
    assert definition.return_type is str
    assert definition.func is greet


def test_a_nested_function_keeps_its_qualified_name():
    async def task() -> None: ...

    assert FunctionDefinition.from_callable(task).name.value.endswith("<locals>.task")


# -- Binding -----------------------------------------------------------------


def test_the_same_call_written_two_ways_binds_the_same():
    definition = FunctionDefinition.from_callable(greet)

    assert definition.bind(("Alice",), {"greeting": "Hi"}) == definition.bind(
        (), {"name": "Alice", "greeting": "Hi"}
    )


def test_a_default_is_bound_as_if_it_had_been_passed():
    # Otherwise calling with and without it would be two identities for one call.
    definition = FunctionDefinition.from_callable(greet)

    assert definition.bind(("Alice",), {}) == {"name": "Alice", "greeting": "Hello"}


def test_variadic_arguments_are_bound_and_so_reach_identity():
    async def task(a: int, *args: int, **kwargs: int) -> None: ...

    definition = FunctionDefinition.from_callable(task)

    assert definition.bind((1, 2), {"x": 3}) == {
        "a": 1,
        "args": (2,),
        "kwargs": {"x": 3},
    }


def test_an_unbindable_call_is_refused():
    definition = FunctionDefinition.from_callable(greet)

    with pytest.raises(TypeError):
        definition.bind((), {})


# -- Type hints --------------------------------------------------------------


def test_a_missing_return_hint_is_refused():
    async def task(a: int): ...

    with pytest.raises(MissingTypeHintError, match="return"):
        FunctionDefinition.from_callable(task)


def test_a_missing_parameter_hint_is_refused():
    async def task(a, b: int) -> None: ...

    with pytest.raises(MissingTypeHintError, match="a"):
        FunctionDefinition.from_callable(task)


def test_a_variadic_parameter_needs_no_hint():
    async def task(a: int, *args, **kwargs) -> None: ...

    assert FunctionDefinition.from_callable(task).return_type is None


def test_a_hint_that_cannot_be_resolved_is_refused():
    async def task(a: "NoSuchType") -> None: ...  # type: ignore[name-defined]  # noqa: F821

    with pytest.raises(TypeHintResolutionError):
        FunctionDefinition.from_callable(task)
