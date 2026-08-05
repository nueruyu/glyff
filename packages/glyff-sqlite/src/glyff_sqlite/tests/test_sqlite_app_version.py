"""A durable store belongs to one generation of code, even under a race."""

import asyncio
from pathlib import Path

import pytest
from glyff import Session, SessionId
from glyff.exceptions import AppVersionMismatchError

from glyff_sqlite import SQLiteBackend


async def _enter(
    db: Path, app_version: str, serializer, argument_canonicalizer
) -> None:
    backend = SQLiteBackend(db)
    async with Session(
        id=SessionId("orders"),
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version=app_version,
    ):
        pass


async def test_concurrent_entries_under_different_versions_admit_one(
    tmp_path: Path, serializer, argument_canonicalizer
):
    db = tmp_path / "race.sqlite3"

    outcomes = await asyncio.gather(
        _enter(db, "v1", serializer, argument_canonicalizer),
        _enter(db, "v2", serializer, argument_canonicalizer),
        return_exceptions=True,
    )

    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], AppVersionMismatchError)


async def test_resuming_under_the_recorded_version_is_accepted(
    tmp_path: Path, serializer, argument_canonicalizer
):
    db = tmp_path / "resume.sqlite3"
    await _enter(db, "v1", serializer, argument_canonicalizer)
    await _enter(db, "v1", serializer, argument_canonicalizer)

    with pytest.raises(AppVersionMismatchError):
        await _enter(db, "v2", serializer, argument_canonicalizer)
