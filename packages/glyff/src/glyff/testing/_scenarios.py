"""pytest conformance contracts for what a store must make a session do.

Re-exported from :mod:`glyff.testing`. Where :mod:`._backend_contract` drives a
`Backend` directly, these drive whole engraved calls through `Session`; see
`docs/backends.md` for why both are worth running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from .._domain import Domain
from .._event_system import EventEmitter, EventHandler
from .._execution import Execution
from .._types import SessionId
from .._interfaces import ArgumentCanonicalizer, Backend, Serializer
from .._session import Session
from ..events import ExecutionCompleted
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
        # Set once a sibling's COMPLETE has been committed. `ExecutionCompleted`
        # is emitted after the transaction closes, so this orders one branch
        # against another without leaning on how long anything takes.
        self.sibling_committed = asyncio.Event()


_state = _Recorder()


class Interrupted(Exception):
    """Stands in for the application exception that pauses a session."""


class ReleaseOnCompletion(EventHandler[ExecutionCompleted]):
    """Lets a waiting branch move once another branch's record is committed."""

    async def handle(self, event: ExecutionCompleted) -> None:
        _state.sibling_committed.set()


# -- Engraved bodies ---------------------------------------------------------
#
# Each returns what its name says, so a test's assertion can be read without
# recomputing anything: what these contracts are about is which calls ran, which
# records remain, and what came back — never arithmetic.

# Enough branches to interleave; the yield below is what forces it, not the count.
CONCURRENT_BRANCHES = 4

# More than one, so a store that could only carry a single record is caught.
RECORDS_PER_SESSION = 3


@engrave
async def doubled(x: int) -> int:
    _state.calls.append(f"doubled({x})")
    if _state.fail:
        raise Interrupted()
    return x * 2


@engrave
async def doubled_plus_one(x: int) -> int:
    return await doubled(x) + 1


@engrave
async def steady() -> str:
    _state.calls.append("steady")
    return "steady"


@engrave
async def pausing() -> str:
    _state.calls.append("pausing:start")
    if _state.interrupt:
        raise Interrupted()
    _state.calls.append("pausing:end")
    return "pausing"


@engrave
async def two_steps() -> str:
    return f"{await steady()}/{await pausing()}"


@engrave
async def branch(index: int) -> int:
    # Yield so siblings genuinely interleave their START/COMPLETE transactions
    # on the shared backend.
    await asyncio.sleep(0)
    _state.calls.append(f"branch({index})")
    return index


@engrave
async def fan_out() -> list[int]:
    branches = list(
        await asyncio.gather(*(branch(i) for i in range(CONCURRENT_BRANCHES)))
    )
    if _state.interrupt:
        raise Interrupted()
    return branches


@engrave
async def completing_sibling() -> str:
    _state.calls.append("completing_sibling")
    return "completing_sibling"


@engrave
async def failing_sibling() -> str:
    if _state.fail:
        # Wait for the sibling's COMPLETE to be committed, so the failure is
        # concurrent with a committed sibling rather than racing it. The timeout
        # is only a failsafe: a backend that never commits should fail, not hang.
        await asyncio.wait_for(_state.sibling_committed.wait(), timeout=5)
        raise Interrupted()
    _state.calls.append("failing_sibling")
    return "failing_sibling"


@engrave
async def two_siblings() -> str:
    # The failing branch is started first and finishes last, so a store that
    # numbered or replayed siblings by the order they completed rather than the
    # order they were called would disagree with itself across the two runs.
    done = await asyncio.gather(failing_sibling(), completing_sibling())
    return "/".join(done)


@engrave
async def only_child() -> str:
    _state.calls.append("only_child")
    return "only_child"


@engrave
async def parent_of_one() -> str:
    value = await only_child()
    if _state.interrupt:
        raise Interrupted()
    return value


@engrave
async def record(index: int) -> dict[str, int]:
    _state.calls.append(f"record({index})")
    # Structured, not a scalar: a store that flattened a result would still pass
    # with a bare number.
    return {"index": index}


@engrave
async def grandchild(side: str) -> str:
    _state.calls.append(f"grandchild({side})")
    return side


@engrave
async def child(side: str) -> str:
    _state.calls.append(f"child({side})")
    await grandchild(side)
    return side


@engrave
async def three_deep() -> str:
    _state.calls.append("three_deep")
    await child("left")
    await child("right")
    return "three_deep"


@engrave
async def finishing_branch() -> str:
    _state.calls.append("finishing_branch")
    await grandchild("finishing")
    return "finished"


@engrave
async def one_branch_finishes_then_another_pauses() -> str:
    return f"{await finishing_branch()}/{await pausing()}"


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
            assert await two_steps() == "steady/pausing"

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
                await parent_of_one()
        assert _state.calls == ["only_child"]

        _state.calls.clear()
        _state.interrupt = False
        async with make_session(
            "scenario-per-event", backend, argument_canonicalizer, serializer
        ):
            assert await parent_of_one() == "only_child"
        assert _state.calls == []

    async def test_a_record_is_replayed_by_a_handle_that_did_not_write_it(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        # An ephemeral store hands back the same object, so it keeps this
        # promise too.
        async with make_session(
            "scenario-reopen",
            backend_factory("scenario-reopen"),
            argument_canonicalizer,
            serializer,
        ):
            written = [await record(i) for i in range(RECORDS_PER_SESSION)]
        _state.calls.clear()

        async with make_session(
            "scenario-reopen",
            backend_factory("scenario-reopen"),
            argument_canonicalizer,
            serializer,
        ):
            assert [await record(i) for i in range(RECORDS_PER_SESSION)] == written
        assert _state.calls == []


class ParallelContract(_SessionScenario):
    """Concurrent branches, which share a backend but not a transaction."""

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
            assert await fan_out() == list(range(CONCURRENT_BRANCHES))

        # Sorted, not a set: every branch ran, and each of them exactly once.
        assert sorted(_state.calls) == [
            f"branch({index})" for index in range(CONCURRENT_BRANCHES)
        ]

    async def test_every_parallel_child_is_durable_after_the_root_is_interrupted(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        # Each branch commits in its own transaction, so this forces siblings to
        # interfere if the backend lets them.
        backend = backend_factory("scenario-parallel-durable")
        _state.interrupt = True
        with pytest.raises(Interrupted):
            async with make_session(
                "scenario-parallel-durable", backend, argument_canonicalizer, serializer
            ):
                await fan_out()
        assert sorted(_state.calls) == [
            f"branch({index})" for index in range(CONCURRENT_BRANCHES)
        ]

        _state.calls.clear()
        _state.interrupt = False
        # A handle that wrote none of them: what a worker picking the session up
        # after the interruption actually holds.
        async with make_session(
            "scenario-parallel-durable",
            backend_factory("scenario-parallel-durable"),
            argument_canonicalizer,
            serializer,
        ):
            assert await fan_out() == list(range(CONCURRENT_BRANCHES))

        # Not one of them ran again: every branch came back from a record this
        # handle did not write.
        assert _state.calls == []

    async def test_a_failed_parallel_child_is_retried_without_rerunning_completed_siblings(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        # One sibling commits, the other fails while that record is already
        # written — a rollback landing next to a committed sibling, not next to
        # one still in flight.
        backend = backend_factory("scenario-parallel-partial")
        _state.fail = True
        with pytest.raises(Interrupted):
            async with make_session(
                "scenario-parallel-partial",
                backend,
                argument_canonicalizer,
                serializer,
                EventEmitter([ReleaseOnCompletion()]),
            ):
                await two_siblings()
        assert _state.calls == ["completing_sibling"]

        _state.calls.clear()
        _state.fail = False
        async with make_session(
            "scenario-parallel-partial",
            backend_factory("scenario-parallel-partial"),
            argument_canonicalizer,
            serializer,
        ):
            assert await two_siblings() == "failing_sibling/completing_sibling"

        # The one that committed came back from its record; only the one that
        # rolled back ran a second time.
        assert _state.calls == ["failing_sibling"]


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
            await three_deep()

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
            await three_deep()

        remaining = await self._executions(backend, "scenario-noprune")
        assert any(execution.id.parent_id is not None for execution in remaining)

    async def test_a_pruned_session_still_replays_from_the_top(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        backend = backend_factory("scenario-prune-replay")
        async with make_session(
            "scenario-prune-replay",
            backend,
            argument_canonicalizer,
            serializer,
            event_emitter=self._pruning(backend),
        ):
            await three_deep()

        _state.calls.clear()
        async with make_session(
            "scenario-prune-replay",
            backend_factory("scenario-prune-replay"),
            argument_canonicalizer,
            serializer,
        ):
            assert await three_deep() == "three_deep"
        assert _state.calls == []

    async def test_a_branch_is_pruned_while_the_session_is_still_running(
        self,
        backend_factory: BackendFactory,
        argument_canonicalizer: ArgumentCanonicalizer,
        serializer: Serializer,
    ):
        # Pruning fires on each completion, not at the end, so a finished branch
        # loses its descendants while a sibling is still to come — and the
        # branch itself must still replay from the record left behind.
        backend = backend_factory("scenario-prune-mid")
        _state.interrupt = True
        with pytest.raises(Interrupted):
            async with make_session(
                "scenario-prune-mid",
                backend,
                argument_canonicalizer,
                serializer,
                event_emitter=self._pruning(backend),
            ):
                await one_branch_finishes_then_another_pauses()

        names = {
            execution.id.name.value
            for execution in await self._executions(backend, "scenario-prune-mid")
        }
        assert {
            "glyff.testing._scenarios.one_branch_finishes_then_another_pauses",
            "glyff.testing._scenarios.finishing_branch",
            "glyff.testing._scenarios.pausing",
        } <= names
        assert "glyff.testing._scenarios.grandchild" not in names

        _state.calls.clear()
        _state.interrupt = False
        reopened = backend_factory("scenario-prune-mid")
        async with make_session(
            "scenario-prune-mid",
            reopened,
            argument_canonicalizer,
            serializer,
            event_emitter=self._pruning(reopened),
        ):
            assert await one_branch_finishes_then_another_pauses() == "finished/pausing"

        assert _state.calls == ["pausing:start", "pausing:end"]
        remaining = await self._executions(reopened, "scenario-prune-mid")
        assert all(execution.id.parent_id is None for execution in remaining)
