"""Store state holds across a real process boundary, not just across tasks.

The in-process lock cannot see another interpreter, so these are the only tests
that exercise what the lock file is actually for.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

from glyff_file_store._file_client import _STORE_FILE
from glyff_file_store._store import FORMAT_VERSION

# Interpreter startup alone staggers the children far more than a claim takes,
# so they announce themselves and then wait on a barrier: without it they would
# serialize by accident and the test would pass with no lock at all.
_CLAIMER = """
import asyncio, json, sys, time
from pathlib import Path
from glyff import DomainId, SessionId
from glyff_file_store import JsonFileBackend

base_dir, version, signals = sys.argv[1], sys.argv[2], Path(sys.argv[3])

async def main() -> None:
    backend = JsonFileBackend(base_dir=base_dir)
    (signals / f"ready-{version}").write_text("")
    while not (signals / "go").exists():
        time.sleep(0.001)
    claimed = await backend.claim_domain(
        SessionId("orders"), DomainId("com.example.payments"), version
    )
    print(json.dumps(claimed))

asyncio.run(main())
"""

_PROCESSES = 8
_VERSIONS = [f"v{index}" for index in range(_PROCESSES)]


def _race(
    script: str, base_dir: Path, signals: Path, arguments: list[str]
) -> list[subprocess.Popen[str]]:
    running = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(base_dir), argument, str(signals)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for argument in arguments
    ]

    deadline = time.monotonic() + 60
    while len(list(signals.glob("ready-*"))) < len(arguments):
        assert time.monotonic() < deadline, "children did not all start"
        time.sleep(0.01)
    (signals / "go").write_text("")
    return running


def _collect(running: list[subprocess.Popen[str]]) -> list:
    outcomes = []
    for process in running:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        outcomes.append(json.loads(stdout))
    return outcomes


def test_processes_racing_one_store_agree_on_one_winner(tmp_path: Path):
    base_dir = tmp_path / "store"
    signals = tmp_path / "signals"
    signals.mkdir()

    outcomes = _collect(_race(_CLAIMER, base_dir, signals, _VERSIONS))

    assert len(outcomes) == _PROCESSES
    assert set(outcomes) <= set(_VERSIONS)
    assert len(set(outcomes)) == 1


_OPENER = """
import json, sys, time
from pathlib import Path
from glyff_file_store import JsonFileBackend

base_dir, name, signals = sys.argv[1], sys.argv[2], Path(sys.argv[3])
(signals / f"ready-{name}").write_text("")
while not (signals / "go").exists():
    time.sleep(0.001)
JsonFileBackend(base_dir=base_dir)
print(json.dumps("opened"))
"""


def test_processes_opening_one_store_leave_a_readable_document(tmp_path: Path):
    base_dir = tmp_path / "store"
    signals = tmp_path / "signals"
    signals.mkdir()

    running = _race(_OPENER, base_dir, signals, [str(n) for n in range(_PROCESSES)])
    assert _collect(running) == ["opened"] * _PROCESSES

    document = json.loads((base_dir / _STORE_FILE).read_text())
    assert document["format_version"] == FORMAT_VERSION
