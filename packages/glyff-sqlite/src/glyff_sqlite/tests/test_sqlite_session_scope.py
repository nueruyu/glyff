"""One set of tables holds one session, and says so when it doesn't."""

from pathlib import Path

import pytest
from glyff.exceptions import StoreSessionMismatchError

from glyff_sqlite import SQLiteBackend


def test_reopening_under_the_same_session_is_accepted(tmp_path: Path):
    db = tmp_path / "same.sqlite3"
    SQLiteBackend(db, session_id="orders")
    SQLiteBackend(db, session_id="orders")


def test_reopening_under_another_session_is_refused(tmp_path: Path):
    # Execution paths carry no session component, so a second session would
    # interleave its records into the first one's history.
    db = tmp_path / "shared.sqlite3"
    SQLiteBackend(db, session_id="orders")

    with pytest.raises(StoreSessionMismatchError, match="'orders'.*'refunds'"):
        SQLiteBackend(db, session_id="refunds")


def test_a_separate_table_prefix_gives_a_second_session_its_own_tables(
    tmp_path: Path,
):
    db = tmp_path / "cohabit.sqlite3"
    SQLiteBackend(db, session_id="orders")
    SQLiteBackend(db, session_id="refunds", table_prefix="refunds")
