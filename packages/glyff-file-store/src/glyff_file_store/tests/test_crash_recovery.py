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
