"""End-to-end pruning against the in-memory repository."""

import pytest

from glyff import ArgumentCanonicalizer, EventEmitter, SessionId, engrave
from glyff.store._memory import _key_to_path
from glyff.testing import PruningEventHandler
from glyff.tests.types import BackendFactory, make_session

_runs: list[str] = []


@pytest.fixture(autouse=True)
def _reset():
    _runs.clear()
    yield
    _runs.clear()


@engrave
async def mp_leaf(n: int) -> int:
    _runs.append(f"leaf{n}")
    return n


@engrave
async def mp_mid(base: int) -> int:
    _runs.append(f"mid{base}")
    return await mp_leaf(base) + await mp_leaf(base + 1)


@engrave
async def mp_root() -> int:
    _runs.append("root")
    return await mp_mid(0) + await mp_mid(10)


def _committed_paths(backend, session_id: str) -> set[str]:
    """Path body of every committed key (depth shows in the '/'-separated path:
    only the root has a depth-1 path)."""
    session = SessionId(session_id)
    return {
        p
        for k in backend.repository._client.data
        if (p := _key_to_path(k, session)) is not None
    }


def _pruning_emitter(backend) -> EventEmitter:
    return EventEmitter([PruningEventHandler(backend.repository)])


async def test_descendant_records_are_gone_after_completion(
    backend_factory: BackendFactory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer,
):
    backend = backend_factory("mem-prune")
    async with make_session(
        "mem-prune",
        backend,
        argument_canonicalizer,
        serializer,
        event_emitter=_pruning_emitter(backend),
    ):
        result = await mp_root()

    assert result == (0 + 1) + (10 + 11)
    assert set(_runs) == {"root", "mid0", "mid10", "leaf0", "leaf1", "leaf10", "leaf11"}

    # Only the root's own records survive — every descendant (any path with a
    # '/') has been deleted.
    paths = _committed_paths(backend, "mem-prune")
    assert paths  # the root is still recorded
    assert all("/" not in p for p in paths)


async def test_disabled_by_default_keeps_descendants(
    backend_factory: BackendFactory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer,
):
    backend = backend_factory("mem-noprune")
    async with make_session("mem-noprune", backend, argument_canonicalizer, serializer):
        await mp_root()

    assert any("/" in p for p in _committed_paths(backend, "mem-noprune"))


async def test_replay_after_prune_does_not_rerun(
    backend_factory: BackendFactory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer,
):
    backend = backend_factory("mem-replay")
    async with make_session(
        "mem-replay",
        backend,
        argument_canonicalizer,
        serializer,
        event_emitter=_pruning_emitter(backend),
    ):
        first = await mp_root()

    _runs.clear()
    # Same backend object => same in-memory data; the completed root short-circuits
    # and the pruned descendants are never needed.
    async with make_session(
        "mem-replay",
        backend,
        argument_canonicalizer,
        serializer,
        event_emitter=_pruning_emitter(backend),
    ):
        second = await mp_root()

    assert second == first
    assert _runs == []
