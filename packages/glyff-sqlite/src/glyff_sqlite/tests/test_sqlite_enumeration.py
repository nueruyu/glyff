"""Enumeration streams the table instead of materializing it."""

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

from glyff import Execution, SessionId, TransactionScope
from glyff.testing import canonical_arguments, make_execution_id

from glyff_sqlite import SQLiteBackend
from glyff_sqlite._sqlite_client import _READ_BATCH_SIZE

SESSION = SessionId("test")


async def test_committed_rows_are_pulled_in_batches(tmp_path: Path):
    # More rows than one batch, consumed one at a time: the point is that a
    # sweep over a production-sized table does not load it all first.
    backend = SQLiteBackend(tmp_path / "batched.sqlite3")
    count = _READ_BATCH_SIZE * 2 + 1

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
        for n in range(_READ_BATCH_SIZE + 5):
            await backend.repository.save(
                SESSION,
                Execution.start(
                    make_execution_id("task", arguments={"n": n}),
                    canonical_arguments({"n": n}),
                ),
            )

    enumeration = cast(
        AsyncGenerator[Execution, None], backend.repository.executions(SESSION)
    )
    assert await anext(enumeration) is not None
    await enumeration.aclose()

    # The abandoned read must not leave the database locked against a writer.
    async with TransactionScope(backend.transaction_provider):
        await backend.repository.save(
            SESSION,
            Execution.start(
                make_execution_id("after", arguments={}), canonical_arguments()
            ),
        )
