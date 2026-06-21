"""End-to-end pruning scenarios driven through Session/executor against the
file store. Proves that registering ``PruningEventHandler`` deletes the history
of a task's descendants once it completes, without changing replay."""

import pytest
from glyff import ArgsHasher, EventEmitter, Serializer, Session, engrave
from glyff.event_handlers import PruningEventHandler

from glyff_file_store import FileClient, JsonFileSessionStore


def _leaf_names(store: JsonFileSessionStore) -> list[str]:
    """Innermost frame name of every committed entry."""
    return [e["call_stack"][-1].split("#")[0] for e in store._log_entries]


def _pruning_emitter() -> EventEmitter:
    return EventEmitter([PruningEventHandler()])


class PruningYield(Exception):
    pass


# --------------------------------------------------------------------------
# Fresh-run pruning
# --------------------------------------------------------------------------

_runs: list[str] = []


@pytest.fixture(autouse=True)
def _reset_runs():
    _runs.clear()
    yield
    _runs.clear()


@engrave
async def pr_leaf(n: int) -> int:
    _runs.append(f"leaf{n}")
    return n


@engrave
async def pr_mid(base: int) -> int:
    _runs.append(f"mid{base}")
    return await pr_leaf(base) + await pr_leaf(base + 1)


@engrave
async def pr_root() -> int:
    _runs.append("root")
    return await pr_mid(0) + await pr_mid(10)


async def test_fresh_run_prunes_whole_subtree(
    tmp_path, serializer: Serializer, hasher: ArgsHasher
):
    store = JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id="prune-fresh"),
        serializer=serializer,
    )
    async with Session(
        id="prune-fresh", store=store, hasher=hasher, event_emitter=_pruning_emitter()
    ):
        result = await pr_root()

    assert result == (0 + 1) + (10 + 11)
    # Everything ran this session...
    assert set(_runs) == {"root", "mid0", "mid10", "leaf0", "leaf1", "leaf10", "leaf11"}
    # ...but only the root's own entries survive — every descendant is pruned.
    assert all(len(e["call_stack"]) == 1 for e in store._log_entries)
    assert set(_leaf_names(store)) == {"pr_root"}


async def test_disabled_flag_retains_descendants(
    tmp_path, serializer: Serializer, hasher: ArgsHasher
):
    store = JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id="prune-off"),
        serializer=serializer,
    )
    async with Session(id="prune-off", store=store, hasher=hasher):  # default: off
        await pr_root()

    # Without pruning the nested history is kept.
    assert any(len(e["call_stack"]) > 1 for e in store._log_entries)
    assert "pr_leaf" in _leaf_names(store)


async def test_replay_after_prune_is_correct(
    tmp_path, serializer: Serializer, hasher: ArgsHasher
):
    sid = "prune-replay"
    store = JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id=sid), serializer=serializer
    )
    async with Session(
        id=sid, store=store, hasher=hasher, event_emitter=_pruning_emitter()
    ):
        first = await pr_root()

    _runs.clear()
    store2 = JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id=sid), serializer=serializer
    )
    async with Session(
        id=sid, store=store2, hasher=hasher, event_emitter=_pruning_emitter()
    ):
        second = await pr_root()

    # Root replays from its own completed record; the pruned children are never
    # needed, so nothing re-runs and the result is identical.
    assert second == first
    assert _runs == []


# --------------------------------------------------------------------------
# Interruption / resume: cache-replayed descendants are still pruned
# --------------------------------------------------------------------------

_sc_runs: list[str] = []
_sc_interrupt: bool = False


@pytest.fixture(autouse=True)
def _reset_sc():
    global _sc_interrupt
    _sc_runs.clear()
    _sc_interrupt = False
    yield
    _sc_runs.clear()
    _sc_interrupt = False


@engrave
async def sc_grand() -> str:
    _sc_runs.append("grand")
    return "G"


@engrave
async def sc_child_a() -> str:
    _sc_runs.append("child_a")
    g = await sc_grand()
    return f"A:{g}"


@engrave
async def sc_child_b() -> str:
    _sc_runs.append("child_b_start")
    if _sc_interrupt:
        raise PruningYield()
    _sc_runs.append("child_b_end")
    return "B"


@engrave
async def sc_root() -> str:
    a = await sc_child_a()
    b = await sc_child_b()
    return f"{a}/{b}"


async def test_nested_completion_prunes_mid_session(
    tmp_path, serializer: Serializer, hasher: ArgsHasher
):
    global _sc_interrupt
    sid = "prune-interrupt"

    # Run 1: child_a completes mid-session and prunes its grandchild right away,
    # even though the root is still running and is then interrupted in child_b.
    # So sc_grand is already gone; child_a/child_b/root frames remain.
    _sc_interrupt = True
    store = JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id=sid), serializer=serializer
    )
    with pytest.raises(PruningYield):
        async with Session(
            id=sid,
            store=store,
            hasher=hasher,
            event_emitter=_pruning_emitter(),
            yield_on=(PruningYield,),
        ):
            await sc_root()

    names = _leaf_names(store)
    assert "sc_root" in names  # STARTED, retained
    assert "sc_child_a" in names  # COMPLETED, retained
    assert "sc_child_b" in names  # STARTED, retained
    assert "sc_grand" not in names  # pruned the moment child_a completed

    # Run 2: resume. child_a is replayed from cache (never re-run), child_b
    # finishes, the root completes -> the root's descendants are pruned,
    # including the cache-replayed child_a/grand this session never re-executed.
    _sc_runs.clear()
    _sc_interrupt = False
    store2 = JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id=sid), serializer=serializer
    )
    async with Session(
        id=sid,
        store=store2,
        hasher=hasher,
        event_emitter=_pruning_emitter(),
        yield_on=(PruningYield,),
    ):
        result = await sc_root()

    assert result == "A:G/B"
    assert "child_a" not in _sc_runs  # replayed from cache
    assert "grand" not in _sc_runs
    assert _sc_runs == ["child_b_start", "child_b_end"]
    # Root completed -> entire subtree pruned, only the root frame remains.
    assert all(len(e["call_stack"]) == 1 for e in store2._log_entries)
    assert set(_leaf_names(store2)) == {"sc_root"}
