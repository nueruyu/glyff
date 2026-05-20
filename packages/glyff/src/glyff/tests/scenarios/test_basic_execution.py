import pytest

from glyff import Session, engrave
from glyff.exceptions import ExecutionFailedError
from glyff.interfaces import ArgsHasher
from glyff.tests.types import StoreFactory

_calls: list[int] = []
_should_fail: bool = False


@pytest.fixture(autouse=True)
def clear_calls():
    global _should_fail
    _calls.clear()
    _should_fail = False
    yield
    _calls.clear()


@engrave
async def basic_simple_func(x: int) -> int:
    _calls.append(x)
    if _should_fail:
        raise ValueError("Intentional failure")
    return x * 2


@engrave
async def basic_parent_func(x: int) -> int:
    y = await basic_simple_func(x)
    _calls.append(y)
    return y + 1


async def test_simple_engrave_returns_correct_result(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("basic-simple")
    async with Session(id="basic-simple", store=store, hasher=hasher):
        result = await basic_simple_func(5)
    assert result == 10
    assert _calls == [5]


async def test_completed_task_is_cached_on_second_session(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("basic-cache")
    async with Session(id="basic-cache", store=store, hasher=hasher):
        await basic_simple_func(7)
    assert _calls == [7]

    _calls.clear()
    async with Session(id="basic-cache", store=store, hasher=hasher):
        result = await basic_simple_func(7)
    assert result == 14
    assert _calls == []  # function body not re-executed


async def test_different_args_produce_independent_results(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("basic-diff-args")
    async with Session(id="basic-diff-args", store=store, hasher=hasher):
        r1 = await basic_simple_func(3)
        r2 = await basic_simple_func(4)
    assert r1 == 6
    assert r2 == 8


async def test_nested_engrave_execution(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("basic-nested")
    async with Session(id="basic-nested", store=store, hasher=hasher):
        result = await basic_parent_func(5)
    assert result == 11  # (5*2) + 1
    assert _calls == [5, 10]


async def test_failed_task_is_not_rerun_and_raises_task_failed_error(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    global _should_fail
    store = store_factory("basic-fail-rerun")
    session_id = "basic-fail-rerun"

    _should_fail = True
    with pytest.raises(ExecutionFailedError):
        async with Session(id=session_id, store=store, hasher=hasher):
            await basic_simple_func(10)
    assert _calls == [10]

    _calls.clear()

    _should_fail = False
    with pytest.raises(ExecutionFailedError):
        async with Session(id=session_id, store=store, hasher=hasher):
            await basic_simple_func(10)

    assert not _calls
