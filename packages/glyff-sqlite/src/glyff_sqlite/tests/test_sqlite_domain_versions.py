"""A durable store's records belong to one generation per domain, even under a race."""

import asyncio
from pathlib import Path

import pytest
from glyff import (
    ArgumentCanonicalizer,
    Domain,
    DomainId,
    DomainVersion,
    Serializer,
    Session,
    SessionId,
)
from glyff.exceptions import DomainVersionMismatchError

from glyff_sqlite import SQLiteBackend

PAYMENTS = DomainId("com.example.payments")


async def _enter(
    db: Path,
    version: str,
    serializer: Serializer,
    argument_canonicalizer: ArgumentCanonicalizer,
) -> None:
    domain = Domain(PAYMENTS, version=DomainVersion(version))

    @domain.engrave
    async def task() -> str:
        return version

    async with Session(
        id=SessionId("orders"),
        backend=SQLiteBackend(db),
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
    ):
        await task()


async def test_concurrent_entries_under_different_versions_admit_one(
    tmp_path: Path,
    serializer: Serializer,
    argument_canonicalizer: ArgumentCanonicalizer,
) -> None:
    db = tmp_path / "race.sqlite3"

    outcomes = await asyncio.gather(
        _enter(db, "v1", serializer, argument_canonicalizer),
        _enter(db, "v2", serializer, argument_canonicalizer),
        return_exceptions=True,
    )

    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], DomainVersionMismatchError)


async def test_resuming_under_the_recorded_version_is_accepted(
    tmp_path: Path,
    serializer: Serializer,
    argument_canonicalizer: ArgumentCanonicalizer,
) -> None:
    db = tmp_path / "resume.sqlite3"
    await _enter(db, "v1", serializer, argument_canonicalizer)
    await _enter(db, "v1", serializer, argument_canonicalizer)

    with pytest.raises(DomainVersionMismatchError):
        await _enter(db, "v2", serializer, argument_canonicalizer)
