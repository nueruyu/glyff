"""End-to-end pruning scenarios driven through Session/executor against the
file store. Proves that registering ``PruningEventHandler`` deletes the history
of a task's descendants once it completes, without changing replay."""

import json

import pytest
from glyff import ArgsHasher, EventEmitter, Session, engrave
from glyff.serialization import JsonSerializer
from glyff.tests.stubs.pruning import PruningEventHandler
from glyff.serialization.constants import DEFAULT_ENCODING

from glyff_file_store import JsonFileSessionStore
from glyff_file_store._file_client import FileClient


class PruningPause(Exception):
    pass


async def _read_executions(store: JsonFileSessionStore) -> dict[str, object]:
    raw = await store._client.read("executions.json")
    if raw is None:
        return {}
    return json.loads(raw.decode(DEFAULT_ENCODING))


async def _leaf_names(store: JsonFileSessionStore) -> list[str]:
    executions = await _read_executions(store)
    return [eid.split("/")[-1].split("#")[0] for eid in executions]


def _pruning_emitter() -> EventEmitter:
    return EventEmitter([PruningEventHandler()])


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
    tmp_path, serializer: JsonSerializer, hasher: ArgsHasher
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
    assert set(_runs) == {"root", "mid0", "mid10", "leaf0", "leaf1", "leaf10", "leaf11"}

    executions = await _read_executions(store)
    assert all("/" not in eid for eid in executions)
    assert set(await _leaf_names(store)) == {"pr_root"}


async def test_disabled_flag_retains_descendants(
    tmp_path, serializer: JsonSerializer, hasher: ArgsHasher
):
    store = JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id="prune-off"),
        serializer=serializer,
    )
    async with Session(id="prune-off", store=store, hasher=hasher):
        await pr_root()

    executions = await _read_executions(store)
    assert any("/" in eid for eid in executions)
    assert "pr_leaf" in await _leaf_names(store)


async def test_replay_after_prune_is_correct(
    tmp_path, serializer: JsonSerializer, hasher: ArgsHasher
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
        raise PruningPause()
    _sc_runs.append("child_b_end")
    return "B"


@engrave
async def sc_root() -> str:
    a = await sc_child_a()
    b = await sc_child_b()
    return f"{a}/{b}"


async def test_nested_completion_prunes_mid_session(
    tmp_path, serializer: JsonSerializer, hasher: ArgsHasher
):
    global _sc_interrupt
    sid = "prune-interrupt"

    _sc_interrupt = True
    store = JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id=sid), serializer=serializer
    )
    with pytest.raises(PruningPause):
        async with Session(
            id=sid, store=store, hasher=hasher, event_emitter=_pruning_emitter()
        ):
            await sc_root()

    names = await _leaf_names(store)
    assert "sc_root" in names
    assert "sc_child_a" in names
    assert "sc_child_b" in names
    assert "sc_grand" not in names

    _sc_runs.clear()
    _sc_interrupt = False
    store2 = JsonFileSessionStore(
        client=FileClient(base_dir=tmp_path, session_id=sid), serializer=serializer
    )
    async with Session(
        id=sid, store=store2, hasher=hasher, event_emitter=_pruning_emitter()
    ):
        result = await sc_root()

    assert result == "A:G/B"
    assert "child_a" not in _sc_runs
    assert "grand" not in _sc_runs
    assert _sc_runs == ["child_b_start", "child_b_end"]

    executions = await _read_executions(store2)
    assert all("/" not in eid for eid in executions)
    assert set(await _leaf_names(store2)) == {"sc_root"}
