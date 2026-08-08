"""pytest conformance contracts for what a store must make a session do.

Re-exported from :mod:`glyff.testing`, the public entry point.

Where :mod:`._backend_contract` drives a `Backend` directly, these drive whole
engraved calls through `Session`, which is where a backend that satisfies every
operation in isolation can still come apart. `docs/backends.md` says what those
failures look like.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from .._domain import Domain
from .._event_system import EventEmitter
from .._execution import Execution
from .._identity import SessionId
from .._interfaces import ArgumentCanonicalizer, Backend, Serializer
from .._session import Session
from ..serialization import JsonArgumentCanonicalizer, JsonSerializer
from ._pruning import PruningEventHandler

BackendFactory = Callable[[str], Backend]

engrave = Domain("glyff.testing", version="1").engrave


def make_session(
    session_id: str | SessionId,
    backend: Backend,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer: Serializer,
    event_emitter: EventEmitter | None = None,
) -> Session:
    """A session over ``backend``, for tests that drive engraved calls."""
    return Session(
        id=SessionId(session_id) if isinstance(session_id, str) else session_id,
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        event_emitter=event_emitter,
    )


class _Recorder:
    """What the engraved bodies below did, and what they should do next.

    Module state, because a function is engraved once at import; the contracts
    reset it around every test.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls: list[str] = []
        self.fail = False
        self.interrupt = False


_state = _Recorder()


class Interrupted(Exception):
    """Stands in for the application exception that pauses a session."""


# -- Engraved bodies ---------------------------------------------------------


@engrave
async def doubled(x: int) -> int:
    _state.calls.append(f"doubled({x})")
    if _state.fail:
        raise Interrupted()
    return x * 2


@engrave
async def doubled_plus_one(x: int) -> int:
    value = await doubled(x)
    _state.calls.append(f"plus_one({value})")
    return value + 1


@engrave
async def pausing() -> str:
    _state.calls.append("pausing:start")
    if _state.interrupt:
        raise Interrupted()
    _state.calls.append("pausing:end")
    return "B"


@engrave
async def steady() -> str:
    _state.calls.append("steady")
    return "A"


@engrave
async def two_steps() -> str:
    return f"{await steady()}:{await pausing()}"


@engrave
async def slow_child(index: int) -> int:
    # Yield so siblings genuinely interleave their START/COMPLETE transactions
    # on the shared backend.
    await asyncio.sleep(0)
    _state.calls.append(f"child({index})")
    return index * 10


@engrave
async def fan_out(width: int) -> int:
    total = sum(await asyncio.gather(*(slow_child(i) for i in range(width))))
    if _state.interrupt:
        raise Interrupted()
    return total


@engrave
async def leaf(n: int) -> int:
    _state.calls.append(f"leaf({n})")
    return n


@engrave
async def middle(base: int) -> int:
    _state.calls.append(f"middle({base})")
    return await leaf(base) + await leaf(base + 1)


@engrave
async def tree() -> int:
    _state.calls.append("tree")
    return await middle(0) + await middle(10)


# -- The contracts -----------------------------------------------------------


class _SessionScenario:
    """Shared wiring: supply ``backend_factory``, override the rest if you need to.

    The serializer and canonicalizer default to the stdlib-only JSON pair,
    because what is under test here is the store, not how values are encoded on
    the way into it.
    """

    @pytest.fixture
    def backend_factory(self) -> BackendFactory:
        raise NotImplementedError

    @pytest.fixture
    def serializer(self) -> Serializer:
        return JsonSerializer()

    @pytest.fixture
    def argument_canonicalizer(self) -> ArgumentCanonicalizer:
        return JsonArgumentCanonicalizer()

    @pytest.fixture(autouse=True)
    def _recorded(self):
        _state.reset()
        yield
        _state.reset()


class EngravedCallContract(_SessionScenario):
    """Recording a call, and what a later session does with the record."""

    async def test_an_engraved_call_returns_what_the_body_returned(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        backend = backend_factory("scenario-simple")
        async with make_session(
            "scenario-simple", backend, argument_canonicalizer, serializer
        ):
            assert await doubled(5) == 10
        assert _state.calls == ["doubled(5)"]

    async def test_a_completed_call_is_replayed_rather_than_re_executed(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        backend = backend_factory("scenario-replay")
        async with make_session(
            "scenario-replay", backend, argument_canonicalizer, serializer
        ):
            await doubled(7)
        _state.calls.clear()

        async with make_session(
            "scenario-replay", backend, argument_canonicalizer, serializer
        ):
            assert await doubled(7) == 14
        assert _state.calls == []

    async def test_calls_with_different_arguments_are_separate_records(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        backend = backend_factory("scenario-args")
        async with make_session(
            "scenario-args", backend, argument_canonicalizer, serializer
        ):
            assert await doubled(3) == 6
            assert await doubled(4) == 8

    async def test_a_nested_call_records_under_its_caller(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        backend = backend_factory("scenario-nested")
        async with make_session(
            "scenario-nested", backend, argument_canonicalizer, serializer
        ):
            assert await doubled_plus_one(5) == 11

        ids = [
            execution.id
            async for execution in backend.repository.executions(
                SessionId("scenario-nested")
            )
        ]
        assert [i for i in ids if i.parent_id is not None]

    async def test_a_call_that_raised_is_tried_again_next_session(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        backend = backend_factory("scenario-retry")
        _state.fail = True
        with pytest.raises(Interrupted):
            async with make_session(
                "scenario-retry", backend, argument_canonicalizer, serializer
            ):
                await doubled(10)
        assert _state.calls == ["doubled(10)"]

        _state.calls.clear()
        _state.fail = False
        async with make_session(
            "scenario-retry", backend, argument_canonicalizer, serializer
        ):
            assert await doubled(10) == 20
        assert _state.calls == ["doubled(10)"]


class ResumeContract(_SessionScenario):
    """What survives an interruption, and what a resumed session skips."""

    async def test_an_interruption_keeps_what_completed_before_it(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        backend = backend_factory("scenario-interrupt")
        _state.interrupt = True
        with pytest.raises(Interrupted):
            async with make_session(
                "scenario-interrupt", backend, argument_canonicalizer, serializer
            ):
                await two_steps()

        assert "steady" in _state.calls
        assert "pausing:start" in _state.calls
        assert "pausing:end" not in _state.calls

    async def test_a_resumed_session_runs_only_what_did_not_finish(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        backend = backend_factory("scenario-resume")
        _state.interrupt = True
        with pytest.raises(Interrupted):
            async with make_session(
                "scenario-resume", backend, argument_canonicalizer, serializer
            ):
                await two_steps()

        _state.calls.clear()
        _state.interrupt = False
        async with make_session(
            "scenario-resume", backend, argument_canonicalizer, serializer
        ):
            assert await two_steps() == "A:B"

        assert "steady" not in _state.calls
        assert "pausing:end" in _state.calls

    async def test_a_completed_child_outlives_its_parents_interruption(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        # Per-event durability: the child was committed as it returned, not when
        # the call it was made from finished.
        backend = backend_factory("scenario-per-event")
        _state.interrupt = True
        with pytest.raises(Interrupted):
            async with make_session(
                "scenario-per-event", backend, argument_canonicalizer, serializer
            ):
                await fan_out(1)
        assert _state.calls == ["child(0)"]

        _state.calls.clear()
        _state.interrupt = False
        async with make_session(
            "scenario-per-event", backend, argument_canonicalizer, serializer
        ):
            assert await fan_out(1) == 0
        assert _state.calls == []

    async def test_a_record_is_replayed_by_a_handle_that_did_not_write_it(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        # A durable store's records outlive the object that wrote them; an
        # ephemeral one hands back the same store, so both keep the promise.
        async with make_session(
            "scenario-reopen",
            backend_factory("scenario-reopen"),
            argument_canonicalizer,
            serializer,
        ):
            assert await doubled(7) == 14
        _state.calls.clear()

        async with make_session(
            "scenario-reopen",
            backend_factory("scenario-reopen"),
            argument_canonicalizer,
            serializer,
        ):
            assert await doubled(7) == 14
        assert _state.calls == []


class ParallelContract(_SessionScenario):
    """Concurrent branches, which share a backend but not a transaction."""

    WIDTH = 12

    async def test_parallel_calls_all_complete(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        backend = backend_factory("scenario-parallel")
        async with make_session(
            "scenario-parallel", backend, argument_canonicalizer, serializer
        ):
            total = await fan_out(self.WIDTH)

        assert total == sum(i * 10 for i in range(self.WIDTH))
        assert len(_state.calls) == self.WIDTH

    async def test_every_parallel_child_is_durable_after_the_root_is_interrupted(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        # Each branch commits in its own transaction, so concurrent siblings must
        # neither flush nor discard each other's staged writes.
        backend = backend_factory("scenario-parallel-durable")
        _state.interrupt = True
        with pytest.raises(Interrupted):
            async with make_session(
                "scenario-parallel-durable", backend, argument_canonicalizer, serializer
            ):
                await fan_out(self.WIDTH)
        assert len(_state.calls) == self.WIDTH

        _state.calls.clear()
        _state.interrupt = False
        async with make_session(
            "scenario-parallel-durable", backend, argument_canonicalizer, serializer
        ):
            total = await fan_out(self.WIDTH)

        assert total == sum(i * 10 for i in range(self.WIDTH))
        assert _state.calls == []


class PruningContract(_SessionScenario):
    """The reference pruning handler, driven through whole sessions."""

    @staticmethod
    async def _executions(backend: Backend, session_id: str) -> list[Execution]:
        return [
            execution
            async for execution in backend.repository.executions(SessionId(session_id))
        ]

    @staticmethod
    def _pruning(backend: Backend) -> EventEmitter:
        return EventEmitter([PruningEventHandler(backend.repository)])

    async def test_a_completed_calls_descendants_are_deleted(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        backend = backend_factory("scenario-prune")
        async with make_session(
            "scenario-prune",
            backend,
            argument_canonicalizer,
            serializer,
            event_emitter=self._pruning(backend),
        ):
            assert await tree() == (0 + 1) + (10 + 11)

        remaining = await self._executions(backend, "scenario-prune")
        assert remaining
        assert all(execution.id.parent_id is None for execution in remaining)

    async def test_nothing_is_pruned_without_the_handler(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        backend = backend_factory("scenario-noprune")
        async with make_session(
            "scenario-noprune", backend, argument_canonicalizer, serializer
        ):
            await tree()

        remaining = await self._executions(backend, "scenario-noprune")
        assert any(execution.id.parent_id is not None for execution in remaining)

    async def test_a_pruned_session_still_replays_from_the_top(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        # The completed root short-circuits, so the deleted descendants are never
        # needed — which is what makes deleting them safe.
        backend = backend_factory("scenario-prune-replay")
        async with make_session(
            "scenario-prune-replay",
            backend,
            argument_canonicalizer,
            serializer,
            event_emitter=self._pruning(backend),
        ):
            first = await tree()

        _state.calls.clear()
        async with make_session(
            "scenario-prune-replay", backend, argument_canonicalizer, serializer
        ):
            assert await tree() == first
        assert _state.calls == []
