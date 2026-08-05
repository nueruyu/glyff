"""End-to-end pruning scenarios driven through Session/executor against the
file store. Proves that registering ``PruningEventHandler`` deletes the history
of a task's descendants once it completes, without changing replay."""

from typing import cast

import pytest
from glyff import ArgumentCanonicalizer, EventEmitter, Session, SessionId, engrave
from glyff.serialization import JsonSerializer
from glyff.testing import PruningEventHandler

from glyff_file_store import FileExecutionRepository, JsonFileBackend
from glyff_file_store._file_client import Executions


class PruningPause(Exception):
    pass


async def _read_execution_map(backend: JsonFileBackend, session_id: str) -> Executions:
    repository = cast(FileExecutionRepository, backend.repository)
    return await repository._client.read_committed_executions(session_id)


async def _leaf_names(backend: JsonFileBackend, session_id: str) -> list[str]:
    execution_map = await _read_execution_map(backend, session_id)
    return [eid.split("/")[-1].split("#")[0] for eid in execution_map]


def _pruning_emitter(backend: JsonFileBackend) -> EventEmitter:
    return EventEmitter([PruningEventHandler(backend.repository)])


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
    tmp_path, serializer: JsonSerializer, argument_canonicalizer: ArgumentCanonicalizer
):
    sid = SessionId("prune-fresh")
    backend = JsonFileBackend(base_dir=tmp_path)
    async with Session(
        id=sid,
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version="test",
        event_emitter=_pruning_emitter(backend),
    ):
        result = await pr_root()

    assert result == (0 + 1) + (10 + 11)
    assert set(_runs) == {"root", "mid0", "mid10", "leaf0", "leaf1", "leaf10", "leaf11"}

    execution_map = await _read_execution_map(backend, sid.value)
    assert all("/" not in eid for eid in execution_map)
    assert set(await _leaf_names(backend, sid.value)) == {"pr_root"}


async def test_disabled_flag_retains_descendants(
    tmp_path, serializer: JsonSerializer, argument_canonicalizer: ArgumentCanonicalizer
):
    sid = SessionId("prune-off")
    backend = JsonFileBackend(base_dir=tmp_path)
    async with Session(
        id=sid,
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version="test",
    ):
        await pr_root()

    execution_map = await _read_execution_map(backend, sid.value)
    assert any("/" in eid for eid in execution_map)
    assert "pr_leaf" in await _leaf_names(backend, sid.value)


async def test_replay_after_prune_is_correct(
    tmp_path, serializer: JsonSerializer, argument_canonicalizer: ArgumentCanonicalizer
):
    sid = SessionId("prune-replay")
    backend = JsonFileBackend(base_dir=tmp_path)
    async with Session(
        id=sid,
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version="test",
        event_emitter=_pruning_emitter(backend),
    ):
        first = await pr_root()

    _runs.clear()
    reopened = JsonFileBackend(base_dir=tmp_path)
    async with Session(
        id=sid,
        backend=reopened,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version="test",
        event_emitter=_pruning_emitter(reopened),
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
    tmp_path, serializer: JsonSerializer, argument_canonicalizer: ArgumentCanonicalizer
):
    global _sc_interrupt
    sid = SessionId("prune-interrupt")

    _sc_interrupt = True
    backend = JsonFileBackend(base_dir=tmp_path)
    with pytest.raises(PruningPause):
        async with Session(
            id=sid,
            backend=backend,
            serializer=serializer,
            argument_canonicalizer=argument_canonicalizer,
            app_version="test",
            event_emitter=_pruning_emitter(backend),
        ):
            await sc_root()

    names = await _leaf_names(backend, sid.value)
    assert "sc_root" in names
    assert "sc_child_a" in names
    assert "sc_child_b" in names
    assert "sc_grand" not in names

    _sc_runs.clear()
    _sc_interrupt = False
    reopened = JsonFileBackend(base_dir=tmp_path)
    async with Session(
        id=sid,
        backend=reopened,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version="test",
        event_emitter=_pruning_emitter(reopened),
    ):
        result = await sc_root()

    assert result == "A:G/B"
    assert "child_a" not in _sc_runs
    assert "grand" not in _sc_runs
    assert _sc_runs == ["child_b_start", "child_b_end"]

    execution_map = await _read_execution_map(reopened, sid.value)
    assert all("/" not in eid for eid in execution_map)
    assert set(await _leaf_names(reopened, sid.value)) == {"sc_root"}
