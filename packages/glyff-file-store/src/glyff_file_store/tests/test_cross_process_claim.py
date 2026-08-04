"""The claim holds across a real process boundary, not just across tasks.

The in-process lock cannot see another interpreter, so this is the only test
that exercises what the lock file is actually for.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

# Interpreter startup alone staggers the children far more than a claim takes,
# so they announce themselves and then wait on a barrier: without it they would
# serialize by accident and the test would pass with no lock at all.
_CLAIMER = """
import asyncio, json, sys, time
from pathlib import Path
from glyff import SessionId
from glyff_file_store import JsonFileBackend

base_dir, app_version, signals = sys.argv[1], sys.argv[2], Path(sys.argv[3])

async def main() -> None:
    backend = JsonFileBackend(base_dir=base_dir)
    (signals / f"ready-{app_version}").write_text("")
    while not (signals / "go").exists():
        time.sleep(0.001)
    print(json.dumps(await backend.claim_session(SessionId("orders"), app_version)))

asyncio.run(main())
"""

_PROCESSES = 8
_VERSIONS = [f"v{index}" for index in range(_PROCESSES)]


def test_processes_racing_one_store_agree_on_one_winner(tmp_path: Path):
    base_dir = tmp_path / "store"
    signals = tmp_path / "signals"
    signals.mkdir()

    running = [
        subprocess.Popen(
            [sys.executable, "-c", _CLAIMER, str(base_dir), version, str(signals)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for version in _VERSIONS
    ]

    deadline = time.monotonic() + 60
    while len(list(signals.glob("ready-*"))) < _PROCESSES:
        assert time.monotonic() < deadline, "children did not all start"
        time.sleep(0.01)
    (signals / "go").write_text("")

    outcomes = []
    for process in running:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        outcomes.append(json.loads(stdout))

    assert len(outcomes) == _PROCESSES
    assert set(outcomes) <= set(_VERSIONS)
    assert len(set(outcomes)) == 1
