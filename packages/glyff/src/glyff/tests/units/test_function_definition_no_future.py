"""The same reading, where annotations are objects rather than strings.

Deliberately without ``from __future__ import annotations``: it changes what
``get_annotations`` is handed, so resolution is worth proving on both sides of it.
"""

import pytest
from glyff._function import FunctionDefinition
from glyff.exceptions import TypeHintResolutionError


class _ModuleScoped:
    pass


def test_a_quoted_hint_is_still_resolved():
    async def task() -> "_ModuleScoped": ...

    assert FunctionDefinition.from_callable(task).return_type is _ModuleScoped


def test_a_quoted_hint_naming_nothing_is_refused():
    async def task() -> "NoSuchType": ...  # type: ignore[name-defined]  # noqa: F821

    with pytest.raises(TypeHintResolutionError, match="Could not resolve type hints"):
        FunctionDefinition.from_callable(task)


def test_an_unquoted_hint_is_the_type_itself():
    async def task() -> _ModuleScoped: ...

    assert FunctionDefinition.from_callable(task).return_type is _ModuleScoped
