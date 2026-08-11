"""A paused session carried across a change of code, end to end.

The migrator computes keys; a resuming session computes them again from the
live call. Only a store this durable can hold the session in between, so the
two halves are proved against each other here rather than in glyff's own tests.
"""

from pathlib import Path

import pytest
from glyff import ArgumentCanonicalizer, Domain, DomainId, Serializer, SessionId
from glyff.exceptions import DomainVersionMismatchError
from glyff.migration import (
    DomainVersionTransition,
    ExecutionMigrator,
    ExecutionShape,
)
from glyff.testing import make_session
from glyff_sqlite import SQLiteBackend

PAY = DomainId("com.example.payments")
V1 = Domain(PAY, version="1")
V2 = Domain(PAY, version="2")

CALLS: list[str] = []
PAUSE = {"at_finish": True}


class Paused(Exception):
    """Stands in for whatever pauses a session."""


# -- The generation that recorded the session --------------------------------
#
# Every boundary is renamed in the next generation, so both live in one module.


@V1.engrave
async def authorize(order: str) -> str:
    CALLS.append("authorize")
    return f"auth:{order}"


@V1.engrave
async def capture(order: str) -> str:
    CALLS.append("capture")
    return f"cap:{order}"


@V1.engrave
async def finish(order: str) -> str:
    CALLS.append("finish")
    if PAUSE["at_finish"]:
        raise Paused()
    return f"done:{order}"


@V1.engrave
async def checkout_v1(order: str) -> str:
    return f"{await authorize(order)}/{await capture(order)}/{await finish(order)}"


# -- The generation that resumes it ------------------------------------------


@V2.engrave
async def charge(order: str) -> str:
    CALLS.append("charge")
    return f"auth:{order}"


@V2.engrave
async def capture_cents(order: str, cents: int) -> str:
    CALLS.append("capture_cents")
    return f"cap:{order}"


@V2.engrave
async def complete(order: str) -> str:
    CALLS.append("complete")
    return f"done:{order}"


@V2.engrave
async def checkout_v2(order: str) -> str:
    return (
        f"{await charge(order)}/"
        f"{await capture_cents(order, 1200)}/"
        f"{await complete(order)}"
    )


def _migration(canonicalizer: ArgumentCanonicalizer) -> ExecutionMigrator:
    migrator = ExecutionMigrator(
        canonicalizer=canonicalizer,
        version_transitions={PAY: DomainVersionTransition("1", "2")},
    )
    migrator.remap(
        ExecutionShape.from_names(PAY, "checkout_v1", "order"),
        ExecutionShape.from_names(PAY, "checkout_v2", "order"),
    )
    migrator.remap(
        ExecutionShape.from_names(PAY, "authorize", "order"),
        ExecutionShape.from_names(PAY, "charge", "order"),
    )
    migrator.remap(
        ExecutionShape.from_names(PAY, "capture", "order"),
        ExecutionShape.from_names(PAY, "capture_cents", "order", "cents"),
        convert_arguments=lambda order: {"order": order, "cents": 1200},
    )
    migrator.remap(
        ExecutionShape.from_names(PAY, "finish", "order"),
        ExecutionShape.from_names(PAY, "complete", "order"),
    )
    return migrator


@pytest.fixture(autouse=True)
def _recorded():
    CALLS.clear()
    PAUSE["at_finish"] = True
    yield
    CALLS.clear()
    PAUSE["at_finish"] = True


async def test_a_migrated_session_resumes_on_the_next_generation(
    tmp_path: Path,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer: Serializer,
):
    database = tmp_path / "payments.sqlite3"
    session = SessionId("order-42")

    async with make_session(
        session, SQLiteBackend(database), argument_canonicalizer, serializer
    ) as _:
        with pytest.raises(Paused):
            await checkout_v1("ord_1")
    assert CALLS == ["authorize", "capture", "finish"]

    # The new code refuses the session rather than replaying its records into a
    # generation nothing agreed on.
    CALLS.clear()
    with pytest.raises(DomainVersionMismatchError):
        async with make_session(
            session, SQLiteBackend(database), argument_canonicalizer, serializer
        ):
            await checkout_v2("ord_1")
    assert CALLS == []

    backend = SQLiteBackend(database)
    report = await backend.session_migration.run(
        session, _migration(argument_canonicalizer)
    )
    assert report.from_domain_versions == {PAY: "1"}
    assert report.to_domain_versions == {PAY: "2"}

    CALLS.clear()
    PAUSE["at_finish"] = False
    # A handle that wrote none of it, as a worker picking the session up holds.
    async with make_session(
        session, SQLiteBackend(database), argument_canonicalizer, serializer
    ):
        assert await checkout_v2("ord_1") == "auth:ord_1/cap:ord_1/done:ord_1"

    # The two that had finished came back from records written under other
    # names, and under other arguments in `capture_cents`' case. Only the one
    # that had not finished ran.
    assert CALLS == ["complete"]
