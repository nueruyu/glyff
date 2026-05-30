import json

from glyff import Session, engrave
from glyff.interfaces import ArgsHasher, Serializer

from glyff_file_store import FileClient, JsonLinesFileSessionStore

_call_count = 0


def reset_count():
    global _call_count
    _call_count = 0


@engrave
async def crash_func() -> str:
    global _call_count
    _call_count += 1
    return "done"


async def test_started_task_is_rerun_on_next_session(
    tmp_path, serializer: Serializer, hasher: ArgsHasher
):
    """Simulates a crash after record_start but before commit by manually
    editing the executions log file to remove the completion record."""
    global _call_count
    _call_count = 0

    session_id = "crash-recovery"
    client = FileClient(base_dir=tmp_path, session_id=session_id)
    store = JsonLinesFileSessionStore(client=client, serializer=serializer)
    log_file = client.resolve("executions.jsonl")

    async with Session(id=session_id, store=store, hasher=hasher):
        await crash_func()
    assert _call_count == 1

    with open(log_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    start_lines = [line for line in lines if json.loads(line)["event_type"] == "start"]

    with open(log_file, "w", encoding="utf-8") as f:
        f.writelines(start_lines)

    _call_count = 0
    client_after_crash = FileClient(base_dir=tmp_path, session_id=session_id)
    store_after_crash = JsonLinesFileSessionStore(
        client=client_after_crash, serializer=serializer
    )
    async with Session(id=session_id, store=store_after_crash, hasher=hasher):
        result = await crash_func()

    assert result == "done"
    assert _call_count == 1


async def test_log_ahead_of_index_is_caught_up(
    tmp_path, serializer: Serializer, hasher: ArgsHasher
):
    """If the log file exists but the index is missing/stale, the next load
    must rebuild the index from the canonical log and avoid re-running any
    already-completed work."""
    global _call_count
    _call_count = 0

    session_id = "catch-up-recovery"
    client = FileClient(base_dir=tmp_path, session_id=session_id)
    store = JsonLinesFileSessionStore(client=client, serializer=serializer)

    async with Session(id=session_id, store=store, hasher=hasher):
        await crash_func()
    assert _call_count == 1

    # Wipe the index file — simulates a crash between log append and index
    # update. The log itself is intact.
    index_file = client.resolve("executions.idx.jsonl")
    index_file.unlink()

    _call_count = 0
    fresh_client = FileClient(base_dir=tmp_path, session_id=session_id)
    fresh_store = JsonLinesFileSessionStore(
        client=fresh_client, serializer=serializer
    )
    async with Session(id=session_id, store=fresh_store, hasher=hasher):
        result = await crash_func()

    assert result == "done"
    assert _call_count == 0  # replayed from rebuilt index, not re-executed
    assert index_file.exists()  # index was rebuilt on load


async def test_corrupted_log_line_is_skipped(
    tmp_path, serializer: Serializer, hasher: ArgsHasher
):
    """A garbage line appended to the log file is logged and skipped, not
    fatal. Previously-committed entries remain accessible."""
    global _call_count
    _call_count = 0

    session_id = "corrupt-log-recovery"
    client = FileClient(base_dir=tmp_path, session_id=session_id)
    store = JsonLinesFileSessionStore(client=client, serializer=serializer)

    async with Session(id=session_id, store=store, hasher=hasher):
        await crash_func()
    assert _call_count == 1

    # Append a garbage line to the log. The index still references the valid
    # entry; the garbage line is past max_indexed_offset and triggers the
    # catch-up path on next load.
    log_file = client.resolve("executions.jsonl")
    with open(log_file, "ab") as f:
        f.write(b"not valid json\n")

    _call_count = 0
    fresh_client = FileClient(base_dir=tmp_path, session_id=session_id)
    fresh_store = JsonLinesFileSessionStore(
        client=fresh_client, serializer=serializer
    )
    async with Session(id=session_id, store=fresh_store, hasher=hasher):
        result = await crash_func()

    assert result == "done"
    assert _call_count == 0  # original entry still loadable
