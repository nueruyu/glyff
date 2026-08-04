from glyff import ArgumentCanonicalizer, Session, engrave
from glyff.serialization import JsonSerializer

from glyff_file_store import JsonFileBackend

_json_runs: list[int] = []


@engrave
async def json_func(n: int) -> int:
    _json_runs.append(n)
    return n * 2


async def test_completed_record_replays_across_instances(
    tmp_path, serializer: JsonSerializer, argument_canonicalizer: ArgumentCanonicalizer
):
    """A completed entry persisted by one store instance must be replayed by
    a fresh instance reading the same on-disk file."""
    _json_runs.clear()
    session_id = "json-replay"

    backend = JsonFileBackend(base_dir=tmp_path)
    async with Session(
        id=session_id,
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
    ):
        first = await json_func(7)
    assert first == 14
    assert _json_runs == [7]

    _json_runs.clear()
    reopened = JsonFileBackend(base_dir=tmp_path)
    async with Session(
        id=session_id,
        backend=reopened,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
    ):
        second = await json_func(7)
    assert second == 14
    assert _json_runs == []


_multi_runs: list[int] = []


@engrave
async def multi_payload(seed: int) -> list[int]:
    _multi_runs.append(seed)
    return [seed + i for i in range(500)]


async def test_multiple_completed_records_replay_across_instances(
    tmp_path, serializer: JsonSerializer, argument_canonicalizer: ArgumentCanonicalizer
):
    """Multiple completed entries with non-trivial payloads must each be
    replayable from a fresh store reading the same on-disk file."""
    _multi_runs.clear()
    session_id = "json-multi"

    backend = JsonFileBackend(base_dir=tmp_path)
    async with Session(
        id=session_id,
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
    ):
        a = await multi_payload(0)
        b = await multi_payload(1000)
        c = await multi_payload(99999)
    assert _multi_runs == [0, 1000, 99999]

    _multi_runs.clear()
    reopened = JsonFileBackend(base_dir=tmp_path)
    async with Session(
        id=session_id,
        backend=reopened,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
    ):
        a2 = await multi_payload(0)
        b2 = await multi_payload(1000)
        c2 = await multi_payload(99999)

    assert a2 == a
    assert b2 == b
    assert c2 == c
    assert _multi_runs == []  # all replayed from disk
