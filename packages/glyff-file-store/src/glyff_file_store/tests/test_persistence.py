from glyff import Session, engrave
from glyff.interfaces import ArgsHasher, Serializer

from glyff_file_store import (
    FileClient,
    JsonFileSessionStore,
    JsonLinesFileSessionStore,
)

_big_runs: list[int] = []


@engrave
async def big_payload(seed: int) -> list[int]:
    _big_runs.append(seed)
    return [seed + i for i in range(500)]


async def test_jsonl_byte_offset_reads_across_large_payloads(
    tmp_path, serializer: Serializer, hasher: ArgsHasher
):
    """Multiple completed entries with non-trivial payloads must each be
    replayable from a fresh store instance, exercising the byte-offset read
    path with non-zero offsets."""
    _big_runs.clear()
    session_id = "jsonl-large"

    client = FileClient(base_dir=tmp_path, session_id=session_id)
    store = JsonLinesFileSessionStore(client=client, serializer=serializer)
    async with Session(id=session_id, store=store, hasher=hasher):
        a = await big_payload(0)
        b = await big_payload(1000)
        c = await big_payload(99999)
    assert _big_runs == [0, 1000, 99999]
    assert a[0] == 0 and a[-1] == 499
    assert b[0] == 1000 and b[-1] == 1499
    assert c[0] == 99999 and c[-1] == 100498

    # Fresh store reads the log via byte offsets in the index, with no
    # results_cache pre-populated.
    _big_runs.clear()
    client2 = FileClient(base_dir=tmp_path, session_id=session_id)
    store2 = JsonLinesFileSessionStore(client=client2, serializer=serializer)
    async with Session(id=session_id, store=store2, hasher=hasher):
        a2 = await big_payload(0)
        b2 = await big_payload(1000)
        c2 = await big_payload(99999)

    assert a2 == a
    assert b2 == b
    assert c2 == c
    assert _big_runs == []  # all replayed from disk via byte offsets


_json_runs: list[int] = []


@engrave
async def json_func(n: int) -> int:
    _json_runs.append(n)
    return n * 2


async def test_json_completed_record_replays_across_instances(
    tmp_path, serializer: Serializer, hasher: ArgsHasher
):
    """Parity coverage for the JSON store: a completed entry persisted by
    one store instance must be replayed by a fresh instance reading the same
    on-disk file."""
    _json_runs.clear()
    session_id = "json-replay"

    client = FileClient(base_dir=tmp_path, session_id=session_id)
    store = JsonFileSessionStore(client=client, serializer=serializer)
    async with Session(id=session_id, store=store, hasher=hasher):
        first = await json_func(7)
    assert first == 14
    assert _json_runs == [7]

    _json_runs.clear()
    client2 = FileClient(base_dir=tmp_path, session_id=session_id)
    store2 = JsonFileSessionStore(client=client2, serializer=serializer)
    async with Session(id=session_id, store=store2, hasher=hasher):
        second = await json_func(7)
    assert second == 14
    assert _json_runs == []
