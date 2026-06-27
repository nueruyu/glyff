"""Parallel per-event durability against the SQLite backend: concurrent
children each commit their completion in an isolated transaction, so after the
root is interrupted every completed child is reused (not re-executed) on resume.
"""

import asyncio
from pathlib import Path

import pytest
from glyff import ArgsHasher, Session, engrave
from glyff.serialization import JsonSerializer

from glyff_sqlite import SQLiteSessionStore

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
async def sqp_child(i: int) -> int:
    await asyncio.sleep(0)
    _ran.add(i)
    return i * 10


@engrave
async def sqp_root() -> int:
    results = await asyncio.gather(*(sqp_child(i) for i in range(_N)))
    total = sum(results)
    if _interrupt_root:
        raise RootInterrupted()
    return total


async def test_sqlite_parallel_children_durable_after_root_interrupt(
    tmp_path: Path, serializer: JsonSerializer, hasher: ArgsHasher
):
    global _interrupt_root
    db = tmp_path / "executions.sqlite3"

    _interrupt_root = True
    with pytest.raises(RootInterrupted):
        async with Session(
            id="sqlite-parallel",
            store=SQLiteSessionStore(db, serializer),
            hasher=hasher,
        ):
            await sqp_root()
    assert _ran == set(range(_N))

    # Re-open the database (fresh store) to prove durability across process-like
    # boundaries, then resume: no child body should re-execute.
    _ran.clear()
    _interrupt_root = False
    async with Session(
        id="sqlite-parallel",
        store=SQLiteSessionStore(db, serializer),
        hasher=hasher,
    ):
        total = await sqp_root()

    assert total == sum(i * 10 for i in range(_N))
    assert _ran == set()
