import asyncio

import pytest

from glyff import ArgsHasher, Session, engrave
from glyff.tests.types import StoreFactory


class ParallelPause(Exception):
    pass


_calls: list[str] = []
_interrupt_b: bool = False


@pytest.fixture(autouse=True)
def reset_state():
    global _interrupt_b
    _calls.clear()
    _interrupt_b = False
    yield
    _calls.clear()
    _interrupt_b = False


@engrave
async def par_a(delay: float) -> str:
    await asyncio.sleep(delay)
    _calls.append("a")
    return "A"


@engrave
async def par_b(delay: float) -> str:
    await asyncio.sleep(delay)
    if _interrupt_b:
        raise ParallelPause()
    _calls.append("b")
    return "B"


@engrave
async def par_root(delay_a: float, delay_b: float) -> str:
    results = await asyncio.gather(
        par_a(delay_a),
        par_b(delay_b),
    )
    return "".join(sorted(results))


async def test_parallel_tasks_complete_successfully(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("par-success")
    async with Session(id="par-success", store=store, hasher=hasher):
        result = await par_root(0.01, 0.005)
    assert result == "AB"
    assert sorted(_calls) == ["a", "b"]


async def test_parallel_interrupted_task_is_retried_on_resume(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    global _interrupt_b
    store = store_factory("par-resume")

    _interrupt_b = True
    with pytest.raises(ParallelPause):
        async with Session(id="par-resume", store=store, hasher=hasher):
            await par_root(0.005, 0.01)

    assert "a" in _calls
    assert "b" not in _calls

    _calls.clear()

    _interrupt_b = False
    async with Session(id="par-resume", store=store, hasher=hasher):
        result = await par_root(0.005, 0.01)

    assert result == "AB"
    assert "a" not in _calls
    assert "b" in _calls
