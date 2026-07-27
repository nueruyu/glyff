from __future__ import annotations

import asyncio
import contextvars
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glyff.exceptions import StoreFormatVersionError

# On-disk schema version, recorded per execution table in the glyff metadata
# table. Bump this when the stored schema changes; opening a table stamped with
# any other version raises StoreFormatVersionError instead of guessing at the
# data.
FORMAT_VERSION = 1

# glyff-owned table that records each execution table's format version. Keyed by
# table name so the store can cohabit an application's database — versioning
# stays in this table and never touches the database's own PRAGMA user_version.
_META_TABLE = "glyff_meta"

_DEFAULT_TABLE_NAME = "glyff_executions"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SQLiteExecutionRecord:
    status: str
    result: str | None
    metadata: str


SQLiteUpdate = Callable[[SQLiteExecutionRecord | None], SQLiteExecutionRecord | None]


@dataclass(frozen=True)
class _Write:
    value: SQLiteExecutionRecord


@dataclass(frozen=True)
class _Delete:
    pass


@dataclass(frozen=True)
class _Update:
    fn: SQLiteUpdate


_StagedOp = _Write | _Delete | _Update

_VALID_SYNCHRONOUS_VALUES = {"OFF", "NORMAL", "FULL", "EXTRA"}


class _SQLiteStagingBuffer:
    __slots__ = ("ops",)

    def __init__(self) -> None:
        self.ops: dict[str, list[_StagedOp]] = {}

    def clear(self) -> None:
        self.ops.clear()


class SQLiteClient:
    """SQLite-backed transactional execution table.

    Each execution is identified by its path and stored in the ``table_name``
    table (default ``glyff_executions``). Operations are staged per transaction
    and committed atomically. A ``glyff_meta`` table records the format version
    of each execution table, so a table written by a different build is refused
    rather than misread — without touching the database's own
    ``PRAGMA user_version``.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 30_000,
        synchronous: str = "FULL",
        table_name: str = _DEFAULT_TABLE_NAME,
    ) -> None:
        synchronous = synchronous.upper()
        if synchronous not in _VALID_SYNCHRONOUS_VALUES:
            valid = ", ".join(sorted(_VALID_SYNCHRONOUS_VALUES))
            raise ValueError(f"synchronous must be one of: {valid}")

        if not _IDENTIFIER_RE.match(table_name):
            raise ValueError(
                "table_name must be a valid SQL identifier "
                "(letters, digits, underscores; not starting with a digit); "
                f"got {table_name!r}."
            )

        if str(database_path) == ":memory:":
            raise ValueError(
                "SQLiteClient does not support ':memory:' because it opens "
                "a new connection per operation."
            )

        self._database_path = Path(database_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._synchronous = synchronous
        self._table_name = table_name
        self._write_lock = asyncio.Lock()
        self._current: contextvars.ContextVar[_SQLiteStagingBuffer | None] = (
            contextvars.ContextVar("sqlite_client_staging", default=None)
        )

        self._database_path.parent.mkdir(parents=True, exist_ok=True)

    # -- Connection helpers ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            isolation_level=None,
            timeout=self._busy_timeout_ms / 1000,
            check_same_thread=False,
        )
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA synchronous={self._synchronous}")
        return connection

    # -- Schema initialization -------------------------------------------------

    def _initialize_schema_sync(self) -> None:
        connection = self._connect()
        in_transaction = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            in_transaction = True
            self._stamp_or_check_format_version(connection)
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{self._table_name}" (
                    path TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    result TEXT,
                    metadata TEXT NOT NULL
                )
                """
            )
            connection.execute("COMMIT")
            in_transaction = False
        except BaseException:
            if in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            connection.close()

    def _stamp_or_check_format_version(self, connection: sqlite3.Connection) -> None:
        # Record the format version in a glyff-owned table keyed by the
        # execution table's name, never in the database's PRAGMA user_version —
        # that belongs to the application when the store cohabits its database.
        # An absent row means this table is new to glyff, so stamp it; any other
        # version was written by a different build, and we refuse it rather than
        # misread the rows.
        connection.execute(
            f'CREATE TABLE IF NOT EXISTS "{_META_TABLE}" ('
            "table_name TEXT PRIMARY KEY, "
            "format_version INTEGER NOT NULL)"
        )
        row = connection.execute(
            f'SELECT format_version FROM "{_META_TABLE}" WHERE table_name = ?',
            (self._table_name,),
        ).fetchone()
        if row is None:
            connection.execute(
                f'INSERT INTO "{_META_TABLE}" (table_name, format_version) '
                "VALUES (?, ?)",
                (self._table_name, FORMAT_VERSION),
            )
        elif row[0] != FORMAT_VERSION:
            raise StoreFormatVersionError(
                f"SQLite store table {self._table_name!r} at "
                f"{self._database_path} has format version {row[0]}, but this "
                f"build of glyff writes version {FORMAT_VERSION}. "
                "Refusing to open it."
            )

    # -- Staging lifecycle -----------------------------------------------------

    def begin_staging(self) -> tuple[contextvars.Token, _SQLiteStagingBuffer]:
        staging = _SQLiteStagingBuffer()
        token = self._current.set(staging)
        return token, staging

    def end_staging(self, token: contextvars.Token) -> None:
        self._current.reset(token)

    def _require_staging(self) -> _SQLiteStagingBuffer:
        staging = self._current.get()
        if staging is None:
            raise RuntimeError("SQLiteClient write attempted outside a transaction.")
        return staging

    def _require_current_staging(self, expected: _SQLiteStagingBuffer) -> None:
        if self._current.get() is not expected:
            raise RuntimeError("Transaction closed out of order.")

    # -- Staging API -----------------------------------------------------------

    def stage_write(self, path: str, value: SQLiteExecutionRecord) -> None:
        staging = self._require_staging()
        staging.ops.setdefault(path, []).append(_Write(value))

    def stage_delete(self, path: str) -> None:
        staging = self._require_staging()
        staging.ops.setdefault(path, []).append(_Delete())

    def stage_update(self, path: str, fn: SQLiteUpdate) -> None:
        staging = self._require_staging()
        staging.ops.setdefault(path, []).append(_Update(fn))

    async def clear_staged(self) -> None:
        self._require_staging().clear()

    # -- Commit ----------------------------------------------------------------

    async def commit_staged(self) -> None:
        staging = self._require_staging()

        if not staging.ops:
            return

        async with self._write_lock:
            await asyncio.to_thread(self._commit_staged_sync, staging)

        staging.clear()

    def _commit_staged_sync(self, staging: _SQLiteStagingBuffer) -> None:
        connection = self._connect()
        in_transaction = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            in_transaction = True

            for path, ops in staging.ops.items():
                current = self._read_value_sync(connection, path)
                result = self._apply_ops(current, ops)

                if result is None:
                    connection.execute(
                        f'DELETE FROM "{self._table_name}" WHERE path = ?',
                        (path,),
                    )
                else:
                    connection.execute(
                        f"""INSERT INTO "{self._table_name}"
                               (path, status, result, metadata)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(path) DO UPDATE SET
                               status = excluded.status,
                               result = excluded.result,
                               metadata = excluded.metadata""",
                        (path, result.status, result.result, result.metadata),
                    )

            connection.execute("COMMIT")
            in_transaction = False
        except BaseException:
            if in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            connection.close()

    @staticmethod
    def _apply_ops(
        data: SQLiteExecutionRecord | None, ops: list[_StagedOp]
    ) -> SQLiteExecutionRecord | None:
        current = data
        for op in ops:
            if isinstance(op, _Write):
                current = op.value
            elif isinstance(op, _Delete):
                current = None
            elif isinstance(op, _Update):
                current = op.fn(current)
            else:
                raise TypeError(f"Unknown SQLite op: {op!r}")
        return current

    # -- Read / list_paths -----------------------------------------------------

    async def read(
        self, path: str, *, staged: bool = True
    ) -> SQLiteExecutionRecord | None:
        committed = await asyncio.to_thread(self._read_committed_sync, path)

        if staged:
            staging = self._current.get()
            if staging is not None:
                ops = staging.ops.get(path)
                if ops:
                    committed = self._apply_ops(committed, ops)

        return committed

    def _read_committed_sync(self, path: str) -> SQLiteExecutionRecord | None:
        connection = self._connect()
        try:
            return self._read_value_sync(connection, path)
        finally:
            connection.close()

    def _read_value_sync(
        self, connection: sqlite3.Connection, path: str
    ) -> SQLiteExecutionRecord | None:
        row = connection.execute(
            f'SELECT status, result, metadata FROM "{self._table_name}" WHERE path = ?',
            (path,),
        ).fetchone()
        if row is None:
            return None
        return SQLiteExecutionRecord(status=row[0], result=row[1], metadata=row[2])

    async def list_paths(self, prefix: str = "", *, staged: bool = True) -> set[str]:
        committed = await asyncio.to_thread(self._list_committed_paths_sync, prefix)

        if staged:
            staging = self._current.get()
            if staging is not None:
                for path, ops in staging.ops.items():
                    if not path.startswith(prefix):
                        continue
                    final = self._apply_ops(
                        await asyncio.to_thread(self._read_committed_sync, path),
                        ops,
                    )
                    if final is None:
                        committed.discard(path)
                    else:
                        committed.add(path)

        return committed

    def _list_committed_paths_sync(self, prefix: str = "") -> set[str]:
        connection = self._connect()
        try:
            if prefix:
                rows = connection.execute(
                    f'SELECT path FROM "{self._table_name}" '
                    "WHERE substr(path, 1, ?) = ?",
                    (len(prefix), prefix),
                ).fetchall()
            else:
                rows = connection.execute(
                    f'SELECT path FROM "{self._table_name}"'
                ).fetchall()
            return {row[0] for row in rows}
        finally:
            connection.close()

    # -- Direct SQL access (for initialization / inspection) -------------------

    async def read_sql(self, sql: str, *params: Any) -> list[tuple]:
        return await asyncio.to_thread(self._read_sql_sync, sql, params)

    def _read_sql_sync(self, sql: str, params: tuple[Any, ...]) -> list[tuple]:
        connection = self._connect()
        try:
            return list(connection.execute(sql, params))
        finally:
            connection.close()

    async def execute(self, sql: str, *params: Any) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._execute_sync, sql, params)

    def _execute_sync(self, sql: str, params: tuple[Any, ...]) -> None:
        connection = self._connect()
        in_transaction = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            in_transaction = True
            connection.execute(sql, params)
            connection.execute("COMMIT")
            in_transaction = False
        except BaseException:
            if in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            connection.close()
