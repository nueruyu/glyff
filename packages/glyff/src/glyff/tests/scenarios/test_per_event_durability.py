"""Per-event durability: each completed call is durable as soon as it returns,
so a completed descendant survives a later interruption of an ancestor and is
reused (not re-executed) on resume."""

import pytest

from glyff import ArgumentCanonicalizer, Domain
from glyff.tests.types import BackendFactory, make_session

engrave = Domain("test", version="1").engrave

_calls: list[str] = []
_interrupt_root: bool = False


class RootInterrupted(Exception):
    pass


@pytest.fixture(autouse=True)
def reset_state():
    global _interrupt_root
    _calls.clear()
    _interrupt_root = False
    yield
    _calls.clear()
    _interrupt_root = False


@engrave
async def ped_child() -> str:
    _calls.append("child")
    return "child"


@engrave
async def ped_root() -> str:
    _calls.append("root")
    value = await ped_child()
    if _interrupt_root:
        raise RootInterrupted()
    return value


async def test_completed_child_is_reused_after_parent_interrupts(
    backend_factory: BackendFactory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer,
):
    global _interrupt_root
    backend = backend_factory("per-event-child-reuse")

    # Run 1: the child completes, then the root is interrupted afterwards.
    _interrupt_root = True
    with pytest.raises(RootInterrupted):
        async with make_session(
            "per-event-child-reuse", backend, argument_canonicalizer, serializer
        ):
            await ped_root()
    assert _calls == ["root", "child"]

    # Run 2: the root re-executes, but the child's completed record is reused
    # rather than re-run — proving the child was made durable before the root
    # was interrupted.
    _calls.clear()
    _interrupt_root = False
    async with make_session(
        "per-event-child-reuse", backend, argument_canonicalizer, serializer
    ):
        result = await ped_root()

    assert result == "child"
    assert _calls == ["root"]  # child body not re-executed
