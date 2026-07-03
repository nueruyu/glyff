"""End-to-end pruning against the (in-memory) scenario store, exercising the
event-driven policy together with the store's get_descendants/delete_executions
mechanism within a live session."""

import pytest

from glyff import ArgsHasher, EventEmitter, Session, engrave
from glyff.store._memory import _key_to_path
from glyff.tests.stubs.pruning import PruningEventHandler
from glyff.tests.types import StoreFactory

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


def _committed_paths(store) -> set[str]:
    """Path body of every committed key (depth shows in the '/'-separated path:
    only the root has a depth-1 path)."""
    return {p for k in store._client.data if (p := _key_to_path(k)) is not None}


def _pruning_emitter() -> EventEmitter:
    return EventEmitter([PruningEventHandler()])


async def test_descendant_records_are_gone_after_completion(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("mem-prune")
    async with Session(
        id="mem-prune", store=store, hasher=hasher, event_emitter=_pruning_emitter()
    ):
        result = await mp_root()

    assert result == (0 + 1) + (10 + 11)
    assert set(_runs) == {"root", "mid0", "mid10", "leaf0", "leaf1", "leaf10", "leaf11"}

    # Only the root's own records survive — every descendant (any path with a
    # '/') has been deleted.
    paths = _committed_paths(store)
    assert paths  # the root is still recorded
    assert all("/" not in p for p in paths)


async def test_disabled_by_default_keeps_descendants(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("mem-noprune")
    async with Session(id="mem-noprune", store=store, hasher=hasher):
        await mp_root()

    assert any("/" in p for p in _committed_paths(store))


async def test_replay_after_prune_does_not_rerun(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    store = store_factory("mem-replay")
    async with Session(
        id="mem-replay", store=store, hasher=hasher, event_emitter=_pruning_emitter()
    ):
        first = await mp_root()

    _runs.clear()
    # Same store object => same in-memory data; the completed root short-circuits
    # and the pruned descendants are never needed.
    async with Session(
        id="mem-replay", store=store, hasher=hasher, event_emitter=_pruning_emitter()
    ):
        second = await mp_root()

    assert second == first
    assert _runs == []
