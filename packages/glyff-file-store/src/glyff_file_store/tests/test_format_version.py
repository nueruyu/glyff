import json
from pathlib import Path

import pytest
from glyff.exceptions import StoreFormatVersionError

from glyff_file_store import JsonFileBackend
from glyff_file_store._store import _FORMAT_FILE, FORMAT_VERSION


def _marker(tmp_path: Path, session_id: str) -> Path:
    return tmp_path / session_id / _FORMAT_FILE


async def test_fresh_session_is_stamped_with_the_format_version(tmp_path: Path):
    JsonFileBackend(base_dir=tmp_path, session_id="stamped")

    marker = _marker(tmp_path, "stamped")
    assert json.loads(marker.read_text()) == {"format_version": FORMAT_VERSION}


async def test_reopening_a_stamped_session_is_accepted(tmp_path: Path):
    JsonFileBackend(base_dir=tmp_path, session_id="reopen")
    JsonFileBackend(base_dir=tmp_path, session_id="reopen")

    marker = _marker(tmp_path, "reopen")
    assert json.loads(marker.read_text())["format_version"] == FORMAT_VERSION


def test_unknown_format_version_is_refused(tmp_path: Path):
    session_dir = tmp_path / "future"
    session_dir.mkdir(parents=True)
    (session_dir / _FORMAT_FILE).write_text(
        json.dumps({"format_version": FORMAT_VERSION + 1})
    )

    with pytest.raises(StoreFormatVersionError):
        JsonFileBackend(base_dir=tmp_path, session_id="future")
