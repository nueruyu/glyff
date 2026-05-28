import asyncio
from collections.abc import AsyncIterator

import pytest

from glyff import Session, engrave
from glyff.exceptions import ExecutionFailedError, YieldException
from glyff.interfaces import ArgsHasher
from glyff.tests.types import StoreFactory

_calls: list[str] = []
_interrupt_at: int | None = None
_fail_at: int | None = None


@pytest.fixture(autouse=True)
def reset_state():
    global _interrupt_at, _fail_at
    _calls.clear()
    _interrupt_at = None
    _fail_at = None
    yield
    _calls.clear()


@engrave
async def streamer(count: int) -> AsyncIterator[str]:
    for i in range(count):
        _calls.append(f"exec_{i}")
        if _interrupt_at is not None and i == _interrupt_at:
            raise YieldException()
        if _fail_at is not None and i == _fail_at:
            raise ValueError(f"Failing at item {i}")
        await asyncio.sleep(0)
        yield f"item_{i}"


@engrave
async def root_streamer(count: int) -> list[str]:
    results: list[str] = []
    async for item in streamer(count):
        _calls.append(f"read_{item}")
        results.append(item)
    return results


async def test_streaming_completes_successfully(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("stream-success")
    async with Session(id="stream-success", store=store, hasher=hasher):
        result = await root_streamer(3)

    assert result == ["item_0", "item_1", "item_2"]
    assert _calls == [
        "exec_0",
        "read_item_0",
        "exec_1",
        "read_item_1",
        "exec_2",
        "read_item_2",
    ]


async def test_streaming_interrupted_and_resumed(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    global _interrupt_at
    store = store_factory("stream-interrupt")

    # Interrupt at i=2: item_0 and item_1 are yielded, item_2 triggers interrupt
    _interrupt_at = 2
    with pytest.raises(YieldException):
        async with Session(id="stream-interrupt", store=store, hasher=hasher):
            await root_streamer(5)

    assert _calls == [
        "exec_0",
        "read_item_0",
        "exec_1",
        "read_item_1",
        "exec_2",
    ]

    _calls.clear()
    _interrupt_at = None

    async with Session(id="stream-interrupt", store=store, hasher=hasher):
        result = await root_streamer(5)

    assert result == ["item_0", "item_1", "item_2", "item_3", "item_4"]
    # Phase 1 replays item_0/item_1 from store (no exec_X for these)
    # Phase 2 re-runs func from i=0, but items 0 and 1 are swallowed (not sent to caller)
    assert _calls == [
        "read_item_0",  # replayed from store
        "read_item_1",  # replayed from store
        "exec_0",  # func re-executed from beginning (item swallowed)
        "exec_1",  # func re-executed (item swallowed)
        "exec_2",  # new item
        "read_item_2",
        "exec_3",
        "read_item_3",
        "exec_4",
        "read_item_4",
    ]


async def test_completed_stream_root_is_cached(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("stream-cache")
    async with Session(id="stream-cache", store=store, hasher=hasher):
        result = await root_streamer(3)
    assert result == ["item_0", "item_1", "item_2"]
    assert "exec_2" in _calls
    _calls.clear()

    # root_streamer is COMPLETED: second run returns cached list without re-executing
    async with Session(id="stream-cache", store=store, hasher=hasher):
        result = await root_streamer(3)

    assert result == ["item_0", "item_1", "item_2"]
    assert not _calls


async def test_streaming_failure_is_recorded_and_persisted(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    global _fail_at
    store = store_factory("stream-fail")

    # Fail at i=1: item_0 is yielded successfully, i=1 raises
    _fail_at = 1
    with pytest.raises(ExecutionFailedError):
        async with Session(id="stream-fail", store=store, hasher=hasher):
            await root_streamer(3)

    assert _calls == ["exec_0", "read_item_0", "exec_1"]
    _calls.clear()
    _fail_at = None

    # On next run root_streamer is FAILED: raises immediately without re-executing
    with pytest.raises(ExecutionFailedError):
        async with Session(id="stream-fail", store=store, hasher=hasher):
            await root_streamer(3)
    assert not _calls
