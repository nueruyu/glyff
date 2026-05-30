from collections.abc import AsyncIterator

from glyff import Session, engrave
from glyff.interfaces import ArgsHasher, Serializer

from glyff_file_store import FileClient, JsonLinesFileSessionStore

_runs: list[int] = []


def reset_runs():
    global _runs
    _runs = []


@engrave
async def fs_stream(n: int) -> AsyncIterator[int]:
    _runs.append(n)
    for i in range(n):
        yield i


async def test_completed_stream_is_replayed_across_store_instances(
    tmp_path, serializer: Serializer, hasher: ArgsHasher
):
    """A stream completed in one process is replayed from the persisted log by a
    fresh store instance, without re-running the producer."""
    reset_runs()
    session_id = "fs-stream-replay"

    client = FileClient(base_dir=tmp_path, session_id=session_id)
    store = JsonLinesFileSessionStore(client=client, serializer=serializer)
    async with Session(id=session_id, store=store, hasher=hasher):
        first = [x async for x in fs_stream(4)]
    assert first == [0, 1, 2, 3]
    assert _runs == [4]

    # Fresh store instance reading the same on-disk log.
    reset_runs()
    client2 = FileClient(base_dir=tmp_path, session_id=session_id)
    store2 = JsonLinesFileSessionStore(client=client2, serializer=serializer)
    async with Session(id=session_id, store=store2, hasher=hasher):
        second = [x async for x in fs_stream(4)]
    assert second == [0, 1, 2, 3]
    assert _runs == []  # replayed from the persisted log, not re-executed


async def test_interrupted_stream_reruns_on_fresh_instance(
    tmp_path, serializer: Serializer, hasher: ArgsHasher
):
    """A stream broken early persists no usable record, so a fresh store instance
    re-runs it from scratch."""
    reset_runs()
    session_id = "fs-stream-break"

    client = FileClient(base_dir=tmp_path, session_id=session_id)
    store = JsonLinesFileSessionStore(client=client, serializer=serializer)
    async with Session(id=session_id, store=store, hasher=hasher):
        async for x in fs_stream(5):
            if x == 1:
                break
    assert _runs == [5]

    reset_runs()
    client2 = FileClient(base_dir=tmp_path, session_id=session_id)
    store2 = JsonLinesFileSessionStore(client=client2, serializer=serializer)
    async with Session(id=session_id, store=store2, hasher=hasher):
        full = [x async for x in fs_stream(5)]
    assert full == [0, 1, 2, 3, 4]
    assert _runs == [5]  # re-run from scratch
