import pytest

from glyff import ArgsCanonicalizer, engrave
from glyff.tests.types import BackendFactory, make_session

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
    backend_factory: BackendFactory, canonicalizer: ArgsCanonicalizer, serializer
):
    global _interrupt
    backend = backend_factory("ir-interrupt")

    _interrupt = True
    with pytest.raises(ApplicationPause, match="waiting"):
        async with make_session("ir-interrupt", backend, canonicalizer, serializer):
            await ir_root()

    assert "a" in _calls
    assert "b_start" in _calls
    assert "b_end" not in _calls


async def test_resumed_session_skips_completed_tasks(
    backend_factory: BackendFactory, canonicalizer: ArgsCanonicalizer, serializer
):
    global _interrupt
    backend = backend_factory("ir-resume")

    _interrupt = True
    with pytest.raises(ApplicationPause, match="waiting"):
        async with make_session("ir-resume", backend, canonicalizer, serializer):
            await ir_root()

    _calls.clear()

    _interrupt = False
    async with make_session("ir-resume", backend, canonicalizer, serializer):
        result = await ir_root()

    assert result == "A:B"
    assert "a" not in _calls
    assert "b_start" in _calls
    assert "b_end" in _calls
