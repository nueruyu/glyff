import pytest

from glyff import ArgumentCanonicalizer, Domain
from glyff.tests.types import BackendFactory, make_session

engrave = Domain("test", version="1").engrave

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
    backend_factory: BackendFactory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer,
):
    backend = backend_factory("basic-simple")
    async with make_session(
        "basic-simple", backend, argument_canonicalizer, serializer
    ):
        result = await basic_simple_func(5)
    assert result == 10
    assert _calls == [5]


async def test_completed_task_is_cached_on_second_session(
    backend_factory: BackendFactory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer,
):
    backend = backend_factory("basic-cache")
    async with make_session("basic-cache", backend, argument_canonicalizer, serializer):
        await basic_simple_func(7)
    assert _calls == [7]

    _calls.clear()
    async with make_session("basic-cache", backend, argument_canonicalizer, serializer):
        result = await basic_simple_func(7)
    assert result == 14
    assert _calls == []  # function body not re-executed


async def test_different_args_produce_independent_results(
    backend_factory: BackendFactory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer,
):
    backend = backend_factory("basic-diff-args")
    async with make_session(
        "basic-diff-args", backend, argument_canonicalizer, serializer
    ):
        r1 = await basic_simple_func(3)
        r2 = await basic_simple_func(4)
    assert r1 == 6
    assert r2 == 8


async def test_nested_engrave_execution(
    backend_factory: BackendFactory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer,
):
    backend = backend_factory("basic-nested")
    async with make_session(
        "basic-nested", backend, argument_canonicalizer, serializer
    ):
        result = await basic_parent_func(5)
    assert result == 11  # (5*2) + 1
    assert _calls == [5, 10]


async def test_failed_task_is_retried_on_next_session(
    backend_factory: BackendFactory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer,
):
    global _should_fail
    backend = backend_factory("basic-fail-rerun")
    session_id = "basic-fail-rerun"

    # A raised exception is non-terminal: it propagates unwrapped but does not
    # permanently poison the call.
    _should_fail = True
    with pytest.raises(ValueError, match="Intentional failure"):
        async with make_session(
            session_id, backend, argument_canonicalizer, serializer
        ):
            await basic_simple_func(10)
    assert _calls == [10]

    _calls.clear()

    # On a later session the previously-interrupted call is retried from scratch.
    _should_fail = False
    async with make_session(session_id, backend, argument_canonicalizer, serializer):
        result = await basic_simple_func(10)

    assert result == 20
    assert _calls == [10]  # body re-executed
