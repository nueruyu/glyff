import pytest

from glyff import ArgsHasher, Session, engrave
from glyff.tests.types import StoreFactory

_calls: list[str] = []
_interrupt: bool = False


class ApplicationPause(Exception):
    pass


@pytest.fixture(autouse=True)
def reset_state():
    global _interrupt
    _calls.clear()
    _interrupt = False
    yield
    _calls.clear()
    _interrupt = False


@engrave
async def ir_a() -> str:
    _calls.append("a")
    return "A"


@engrave
async def ir_b() -> str:
    _calls.append("b_start")
    if _interrupt:
        raise ApplicationPause("waiting")
    _calls.append("b_end")
    return "B"


@engrave
async def ir_root() -> str:
    a = await ir_a()
    b = await ir_b()
    return f"{a}:{b}"


async def test_interrupted_session_engraves_completed_tasks(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    global _interrupt
    store = store_factory("ir-interrupt")

    _interrupt = True
    with pytest.raises(ApplicationPause, match="waiting"):
        async with Session(id="ir-interrupt", store=store, hasher=hasher):
            await ir_root()

    assert "a" in _calls
    assert "b_start" in _calls
    assert "b_end" not in _calls


async def test_resumed_session_skips_completed_tasks(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    global _interrupt
    store = store_factory("ir-resume")

    _interrupt = True
    with pytest.raises(ApplicationPause, match="waiting"):
        async with Session(id="ir-resume", store=store, hasher=hasher):
            await ir_root()

    _calls.clear()

    _interrupt = False
    async with Session(id="ir-resume", store=store, hasher=hasher):
        result = await ir_root()

    assert result == "A:B"
    assert "a" not in _calls
    assert "b_start" in _calls
    assert "b_end" in _calls
