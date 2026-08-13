"""What decorating adds on top of reading the function.

Reading it — signatures, hints, binding — belongs to `FunctionDefinition` and is
tested there.
"""

from __future__ import annotations

import pytest

from glyff import Domain, Session, SessionId
from glyff.exceptions import ArgumentCanonicalizationError, MissingTypeHintError
from glyff.store import MemoryBackend

engrave = Domain("test", version="1").engrave


def test_a_function_glyff_cannot_read_is_refused_at_decoration():
    # Not at the first call: a definition glyff cannot record is the author's
    # mistake, and it should surface where the author wrote it.
    with pytest.raises(MissingTypeHintError):

        @engrave
        async def func(arg) -> str:
            return "hello"


def test_a_readable_function_comes_back_callable():
    @engrave
    async def func(arg: int) -> str:
        return str(arg)

    assert callable(func)


def test_an_invalid_explicit_name_is_refused_at_decoration():
    with pytest.raises(ValueError, match="valid explicit execution name"):

        @engrave(name="not a name")
        async def func() -> None: ...


async def test_an_explicit_name_is_recorded(serializer, argument_canonicalizer):
    backend = MemoryBackend()

    @engrave(name="billing.charge")
    async def implementation_name() -> str:
        return "charged"

    async with Session(
        id=SessionId("explicit-name"),
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
    ):
        assert await implementation_name() == "charged"

    [execution] = [
        execution
        async for execution in backend.repository.executions(SessionId("explicit-name"))
    ]
    assert execution.id.name.value == "billing.charge"


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
