from __future__ import annotations

import pytest

from glyff import engrave


class _MyClass:
    pass


def test_engrave_raises_for_unresolvable_type_hint():
    with pytest.raises(TypeError, match="Could not resolve type hints"):

        @engrave
        async def func() -> UndefinedType:  # type: ignore[name-defined]  # noqa: F821
            return "hello"


def test_engrave_raises_for_unresolvable_type_hint_in_args():
    with pytest.raises(TypeError, match="Could not resolve type hints"):

        @engrave
        async def func(arg: SomeClass) -> AnotherClass:  # type: ignore[name-defined]  # noqa: F821
            pass


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
