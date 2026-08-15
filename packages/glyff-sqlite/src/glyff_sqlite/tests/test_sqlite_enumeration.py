"""Enumeration across the batch boundary of the range scan."""

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

from glyff import Execution, SessionId, TransactionScope
from glyff.testing import canonical_arguments, make_execution_id

from glyff_sqlite import SQLiteBackend
from glyff_sqlite._sqlite_client import READ_BATCH_SIZE

SESSION = SessionId("test")


async def test_enumeration_returns_rows_across_batch_boundaries(tmp_path: Path):
    backend = SQLiteBackend(tmp_path / "batched.sqlite3")
    count = READ_BATCH_SIZE * 2 + 1

    async with TransactionScope(backend.transaction_provider):
        for n in range(count):
            await backend.repository.save(
                SESSION,
                Execution.start(
                    make_execution_id("task", arguments={"n": n}),
                    canonical_arguments({"n": n}),
                ),
            )

    seen = 0
    async for _ in backend.repository.executions(SESSION):
        seen += 1
    assert seen == count


async def test_a_partially_consumed_enumeration_closes_its_connection(
    tmp_path: Path,
):
    backend = SQLiteBackend(tmp_path / "partial.sqlite3")
    async with TransactionScope(backend.transaction_provider):
        for n in range(READ_BATCH_SIZE + 5):
            await backend.repository.save(
                SESSION,
                Execution.start(
                    make_execution_id("task", arguments={"n": n}),
                    canonical_arguments({"n": n}),
                ),
            )

    # Closed explicitly rather than left to the collector, so the abandoned read
    # is known to have released the database before the write below.
    enumeration = cast(
        AsyncGenerator[Execution, None], backend.repository.executions(SESSION)
    )
    assert await anext(enumeration) is not None
    await enumeration.aclose()

    async with TransactionScope(backend.transaction_provider):
        await backend.repository.save(
            SESSION,
            Execution.start(
                make_execution_id("after", arguments={}), canonical_arguments()
            ),
        )
