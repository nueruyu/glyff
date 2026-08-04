"""Parallel per-event durability against the JSON debug backend: per-transaction
staging plus serialized whole-file commits keep concurrent children isolated, so
after the root is interrupted every completed child is reused (not re-executed)
on resume."""

import asyncio

import pytest
from glyff import ArgumentCanonicalizer, Session, SessionId, engrave
from glyff.serialization import JsonSerializer

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
async def jp_child(i: int) -> int:
    await asyncio.sleep(0)
    _ran.add(i)
    return i * 10


@engrave
async def jp_root() -> int:
    results = await asyncio.gather(*(jp_child(i) for i in range(_N)))
    total = sum(results)
    if _interrupt_root:
        raise RootInterrupted()
    return total


async def test_json_parallel_children_durable_after_root_interrupt(
    backend_factory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer: JsonSerializer,
):
    global _interrupt_root
    sid = SessionId("json-parallel")

    _interrupt_root = True
    with pytest.raises(RootInterrupted):
        backend = backend_factory(sid.value)
        async with Session(
            id=sid,
            backend=backend,
            serializer=serializer,
            argument_canonicalizer=argument_canonicalizer,
            app_version="test",
        ):
            await jp_root()
    assert _ran == set(range(_N))

    # Fresh store over the same session directory, then resume: no child re-runs.
    _ran.clear()
    _interrupt_root = False
    backend = backend_factory(sid.value)
    async with Session(
        id=sid,
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version="test",
    ):
        total = await jp_root()

    assert total == sum(i * 10 for i in range(_N))
    assert _ran == set()
