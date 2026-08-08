from __future__ import annotations

import pytest

from glyff import Domain, Session, SessionId
from glyff.exceptions import (
    ArgumentCanonicalizationError,
    MissingTypeHintError,
    TypeHintResolutionError,
)
from glyff.store import MemoryBackend

engrave = Domain("test", version="1").engrave


class _MyClass:
    pass


def test_engrave_raises_for_unresolvable_type_hint():
    with pytest.raises(TypeHintResolutionError, match="Could not resolve type hints"):

        @engrave
        async def func() -> UndefinedType:  # type: ignore[name-defined]  # noqa: F821
            return "hello"


def test_engrave_raises_for_unresolvable_type_hint_in_args():
    with pytest.raises(TypeHintResolutionError, match="Could not resolve type hints"):

        @engrave
        async def func(arg: SomeClass) -> AnotherClass:  # type: ignore[name-defined]  # noqa: F821
            pass


def test_engrave_raises_for_missing_return_type_hint():
    with pytest.raises(MissingTypeHintError, match="return"):

        @engrave
        async def func(arg: int):
            pass


def test_engrave_raises_for_missing_argument_type_hint():
    with pytest.raises(MissingTypeHintError, match="arg"):

        @engrave
        async def func(arg) -> str:
            return "hello"


def test_engrave_reports_missing_type_hint_before_resolution_error():
    with pytest.raises(MissingTypeHintError, match="arg"):

        @engrave
        async def func(arg) -> UndefinedType:  # type: ignore[name-defined]  # noqa: F821
            return "hello"


def test_engrave_allows_unannotated_self_parameter():
    class Service:
        @engrave
        async def method(self, arg: int) -> str:
            return str(arg)

    assert Service().method is not None


def test_engrave_allows_unannotated_cls_parameter():
    class Service:
        @classmethod
        @engrave
        async def method(cls, arg: int) -> str:
            return str(arg)

    assert Service.method is not None


def test_engrave_allows_unannotated_var_args_and_kwargs():
    @engrave
    async def func(*args, **kwargs) -> str:
        return "hello"

    assert func is not None


def test_engrave_succeeds_when_return_type_is_resolvable():
    @engrave
    async def func() -> str:
        return "hello"

    assert func is not None


def test_engrave_succeeds_with_type_in_module_scope():
    # engrave resolves type hints eagerly at decoration time using module globals.
    # _MyClass is defined at module level, so it is resolvable regardless of
    # whether __future__ annotations is active.
    @engrave
    async def func() -> _MyClass:
        return _MyClass()

    assert func is not None


def test_engrave_succeeds_with_string_annotation_resolved_from_module_scope():
    # A quoted annotation is also resolved against module globals at decoration time.
    @engrave
    async def func() -> "_MyClass":
        return _MyClass()

    assert func is not None


async def test_an_uncanonicalizable_argument_names_the_function_it_was_passed_to(
    serializer, argument_canonicalizer
):
    # The canonicalizer is handed values, not the call they came from, so this
    # context exists only if the engraved wrapper adds it.
    @engrave
    async def send(payload: object) -> None: ...

    session = Session(
        id=SessionId("uncanonicalizable"),
        backend=MemoryBackend(),
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
    )

    async with session:
        with pytest.raises(ArgumentCanonicalizationError, match="'.*send'"):
            await send(object())
