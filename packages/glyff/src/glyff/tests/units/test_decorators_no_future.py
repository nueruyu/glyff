import pytest

from glyff import Domain
from glyff.exceptions import TypeHintResolutionError

engrave = Domain("test", version="1").engrave


class _MyClass:
    pass


def test_engrave_raises_for_unresolvable_string_annotation():
    # Without __future__ annotations, quoted type hints are still evaluated
    # lazily by get_annotations(eval_str=True), so an undefined name raises a
    # custom type-hint resolution error.
    with pytest.raises(TypeHintResolutionError, match="Could not resolve type hints"):

        @engrave
        async def func() -> "UndefinedClass":  # type: ignore[name-defined]  # noqa: F821
            pass


def test_engrave_succeeds_with_resolvable_string_annotation():
    @engrave
    async def func() -> "_MyClass":
        return _MyClass()

    assert func is not None
