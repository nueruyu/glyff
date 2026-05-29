from collections.abc import AsyncGenerator, AsyncIterator

import pytest

from glyff import Session, engrave
from glyff.exceptions import ExecutionFailedError
from glyff.interfaces import ArgsHasher
from glyff.tests.types import StoreFactory

# Records each time a producer body actually runs (i.e. is not replayed).
_runs: list[int] = []
_sib_runs: list[int] = []


@pytest.fixture(autouse=True)
def reset_state():
    _runs.clear()
    _sib_runs.clear()
    yield
    _runs.clear()
    _sib_runs.clear()


@engrave
async def stream_numbers(n: int) -> AsyncIterator[int]:
    _runs.append(n)
    for i in range(n):
        yield i


@engrave
async def stream_numbers_ag(n: int) -> AsyncGenerator[int, None]:
    _runs.append(n)
    for i in range(n):
        yield i


@engrave
async def stream_explodes(n: int) -> AsyncIterator[int]:
    _runs.append(n)
    for i in range(n):
        if i == 2:
            raise ValueError("boom")
        yield i


@engrave
async def sib(x: int) -> int:
    _sib_runs.append(x)
    return x * 10


async def test_streaming_yields_all_items_transparently(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("stream-basic")
    async with Session(id="stream-basic", store=store, hasher=hasher):
        items = [x async for x in stream_numbers(4)]
    assert items == [0, 1, 2, 3]
    assert _runs == [4]


async def test_completed_stream_is_replayed_without_rerun(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("stream-replay")
    async with Session(id="stream-replay", store=store, hasher=hasher):
        first = [x async for x in stream_numbers(3)]
    assert first == [0, 1, 2]
    assert _runs == [3]

    _runs.clear()
    async with Session(id="stream-replay", store=store, hasher=hasher):
        second = [x async for x in stream_numbers(3)]
    assert second == [0, 1, 2]  # same items
    assert _runs == []  # producer body not re-executed; replayed from store


async def test_async_generator_annotation_is_detected_as_streaming(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    # If `AsyncGenerator[...]` were not detected as streaming, the non-streaming
    # wrapper would try to `await` the async generator and fail. A clean stream
    # plus replay proves detection works for the `AsyncGenerator` origin too.
    store = store_factory("stream-ag")
    async with Session(id="stream-ag", store=store, hasher=hasher):
        first = [x async for x in stream_numbers_ag(3)]
    assert first == [0, 1, 2]

    _runs.clear()
    async with Session(id="stream-ag", store=store, hasher=hasher):
        second = [x async for x in stream_numbers_ag(3)]
    assert second == [0, 1, 2]
    assert _runs == []


async def test_early_break_is_not_persisted_and_reruns(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("stream-break")
    async with Session(id="stream-break", store=store, hasher=hasher):
        partial = []
        async for x in stream_numbers(5):
            partial.append(x)
            if x == 1:
                break
    assert partial == [0, 1]
    assert _runs == [5]

    # Nothing was persisted, so a later full consumption re-runs from scratch.
    _runs.clear()
    async with Session(id="stream-break", store=store, hasher=hasher):
        full = [x async for x in stream_numbers(5)]
    assert full == [0, 1, 2, 3, 4]
    assert _runs == [5]  # producer ran again


async def test_sibling_completion_survives_early_break_of_stream(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    # While consuming a stream, the caller completes sibling `@engrave` calls and
    # then breaks early. Those completions must remain durable (the stream's
    # discard must not roll them back).
    store = store_factory("stream-sibling")
    async with Session(id="stream-sibling", store=store, hasher=hasher):
        async for x in stream_numbers(5):
            await sib(x)
            if x == 1:
                break
    assert _sib_runs == [0, 1]

    _sib_runs.clear()
    async with Session(id="stream-sibling", store=store, hasher=hasher):
        r0 = await sib(0)
        r1 = await sib(1)
    assert (r0, r1) == (0, 10)
    assert _sib_runs == []  # both replayed from store, not re-run


async def test_stream_error_records_failure_and_raises_on_replay(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("stream-error")

    with pytest.raises(ExecutionFailedError):
        async with Session(id="stream-error", store=store, hasher=hasher):
            async for _ in stream_explodes(5):
                pass
    assert _runs == [5]

    # The failure is permanent: re-invoking raises without re-running the body.
    _runs.clear()
    with pytest.raises(ExecutionFailedError):
        async with Session(id="stream-error", store=store, hasher=hasher):
            async for _ in stream_explodes(5):
                pass
    assert _runs == []


async def test_caller_exception_does_not_record_failure(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    # A caller-side exception during consumption must not mark the stream FAILED:
    # the *same* call must remain runnable afterwards. (Uses matching args so the
    # second consumption targets the same execution record.)
    store = store_factory("stream-caller-error")

    with pytest.raises(ValueError, match="caller boom"):
        async with Session(id="stream-caller-error", store=store, hasher=hasher):
            async for x in stream_numbers(5):
                if x == 1:
                    raise ValueError("caller boom")

    _runs.clear()
    async with Session(id="stream-caller-error", store=store, hasher=hasher):
        full = [x async for x in stream_numbers(5)]
    assert full == [0, 1, 2, 3, 4]
    assert _runs == [5]  # not poisoned by the caller error; re-runs and completes
