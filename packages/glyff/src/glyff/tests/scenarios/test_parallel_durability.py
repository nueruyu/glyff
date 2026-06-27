"""Per-event durability must stay correct under parallel fan-out: each branch's
completion is committed in its own isolated transaction, so concurrent siblings
never flush or discard each other's staged writes. After the root is
interrupted, every completed child must be reused (not re-executed) on resume."""

import asyncio

import pytest

from glyff import ArgsHasher, Session, engrave
from glyff.tests.types import StoreFactory

_ran: set[int] = set()
_interrupt_root: bool = False
_N = 12


class RootInterrupted(Exception):
    pass


@pytest.fixture(autouse=True)
def reset_state():
    global _interrupt_root
    _ran.clear()
    _interrupt_root = False
    yield
    _ran.clear()
    _interrupt_root = False


@engrave
async def pard_child(i: int) -> int:
    # Yield control so siblings genuinely interleave their START/COMPLETE
    # transactions on the shared store.
    await asyncio.sleep(0)
    _ran.add(i)
    return i * 10


@engrave
async def pard_root() -> int:
    results = await asyncio.gather(*(pard_child(i) for i in range(_N)))
    total = sum(results)
    if _interrupt_root:
        raise RootInterrupted()
    return total


async def test_parallel_children_are_each_durable_after_root_interrupt(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    global _interrupt_root
    store = store_factory("parallel-durability")

    # Run 1: all children complete in parallel, then the root is interrupted.
    _interrupt_root = True
    with pytest.raises(RootInterrupted):
        async with Session(id="parallel-durability", store=store, hasher=hasher):
            await pard_root()
    assert _ran == set(range(_N))

    # Run 2: the root re-executes, but every child's completed record is reused
    # rather than re-run — none was lost to a concurrent sibling's commit.
    _ran.clear()
    _interrupt_root = False
    async with Session(id="parallel-durability", store=store, hasher=hasher):
        total = await pard_root()

    assert total == sum(i * 10 for i in range(_N))
    assert _ran == set()  # no child body re-executed
