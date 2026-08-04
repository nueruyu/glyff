from __future__ import annotations

import asyncio
import contextvars
import re
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glyff.exceptions import StoreFormatVersionError

# Bump when the stored schema changes.
FORMAT_VERSION = 1

# The store's tables are derived from one prefix, so the version lives in a
# table glyff owns rather than the database-wide PRAGMA user_version.
_DEFAULT_TABLE_PREFIX = "glyff"
_EXECUTIONS_SUFFIX = "_executions"
_SESSIONS_SUFFIX = "_sessions"
_META_SUFFIX = "_meta"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SQLiteExecutionRecord:
    arguments: str
    status: str
    result: str | None
    metadata: str


SQLiteUpdate = Callable[[SQLiteExecutionRecord | None], SQLiteExecutionRecord | None]

# Rows are staged under the session that owns them, so one transaction can span
# sessions without their paths colliding.
SQLiteKey = tuple[str, str]


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

# Rows per fetch while streaming a range scan.
_READ_BATCH_SIZE = 256


def _prefix_upper_bound(prefix: str) -> str:
    """The least string ordering above every string that starts with ``prefix``.

    SQLite compares TEXT by UTF-8 bytes, which order the same as code points, so
    incrementing the last one bounds the range.
    """
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


class _SQLiteStagingBuffer:
    __slots__ = ("ops",)

    def __init__(self) -> None:
        self.ops: dict[SQLiteKey, list[_StagedOp]] = {}

    def clear(self) -> None:
        self.ops.clear()


class SQLiteClient:
    """SQLite-backed transactional execution table.

    Each execution is identified by its path and stored in the
    ``<table_prefix>_executions`` table (default prefix ``glyff``). Operations
    are staged per transaction and committed atomically. A sibling
    ``<table_prefix>_sessions`` table records the application version behind each
    session's records, and ``<table_prefix>_meta`` holds the store's own format
    version in a single row.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 30_000,
        synchronous: str = "FULL",
        table_prefix: str = _DEFAULT_TABLE_PREFIX,
    ) -> None:
        synchronous = synchronous.upper()
        if synchronous not in _VALID_SYNCHRONOUS_VALUES:
            valid = ", ".join(sorted(_VALID_SYNCHRONOUS_VALUES))
            raise ValueError(f"synchronous must be one of: {valid}")

        if not _IDENTIFIER_RE.match(table_prefix):
            raise ValueError(
                "table_prefix must be a valid SQL identifier "
                "(letters, digits, underscores; not starting with a digit); "
                f"got {table_prefix!r}."
            )

        # SQLite reserves object names starting with "sqlite_".
        if table_prefix.lower() == "sqlite" or table_prefix.lower().startswith(
            "sqlite_"
        ):
            raise ValueError(
                "table_prefix may not be 'sqlite' or start with 'sqlite_' "
                f"(reserved by SQLite for internal use); got {table_prefix!r}."
            )

        if str(database_path) == ":memory:":
            raise ValueError(
                "SQLiteClient does not support ':memory:' because it opens "
                "a new connection per operation."
            )

        self._database_path = Path(database_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._synchronous = synchronous
        self._table_name = table_prefix + _EXECUTIONS_SUFFIX
        self._sessions_table_name = table_prefix + _SESSIONS_SUFFIX
        self._meta_table_name = table_prefix + _META_SUFFIX
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
            self._stamp_or_check_meta(connection)
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{self._table_name}" (
                    session_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    arguments TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    metadata TEXT NOT NULL,
                    PRIMARY KEY (session_id, path)
                )
                """
            )
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{self._sessions_table_name}" (
                    session_id TEXT PRIMARY KEY,
                    app_version TEXT NOT NULL
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

    def _stamp_or_check_meta(self, connection: sqlite3.Connection) -> None:
        # No row means a store glyff has never stamped, which it adopts as current.
        # The CHECK keeps the row unique, so the read below cannot be ambiguous.
        connection.execute(
            f'CREATE TABLE IF NOT EXISTS "{self._meta_table_name}" ('
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "format_version INTEGER NOT NULL)"
        )
        row = connection.execute(
            f'SELECT format_version FROM "{self._meta_table_name}" WHERE id = 1'
        ).fetchone()
        if row is None:
            connection.execute(
                f'INSERT INTO "{self._meta_table_name}" (id, format_version) '
                "VALUES (1, ?)",
                (FORMAT_VERSION,),
            )
            return

        if row[0] != FORMAT_VERSION:
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

    def stage_write(self, key: SQLiteKey, value: SQLiteExecutionRecord) -> None:
        staging = self._require_staging()
        staging.ops.setdefault(key, []).append(_Write(value))

    def stage_delete(self, key: SQLiteKey) -> None:
        staging = self._require_staging()
        staging.ops.setdefault(key, []).append(_Delete())

    def stage_update(self, key: SQLiteKey, fn: SQLiteUpdate) -> None:
        staging = self._require_staging()
        staging.ops.setdefault(key, []).append(_Update(fn))

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

            for key, ops in staging.ops.items():
                session_id, path = key
                current = self._read_value_sync(connection, key)
                result = self._apply_ops(current, ops)

                if result is None:
                    connection.execute(
                        f'DELETE FROM "{self._table_name}" '
                        "WHERE session_id = ? AND path = ?",
                        (session_id, path),
                    )
                else:
                    connection.execute(
                        f"""INSERT INTO "{self._table_name}"
                               (session_id, path, arguments, status, result, metadata)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(session_id, path) DO UPDATE SET
                               arguments = excluded.arguments,
                               status = excluded.status,
                               result = excluded.result,
                               metadata = excluded.metadata""",
                        (
                            session_id,
                            path,
                            result.arguments,
                            result.status,
                            result.result,
                            result.metadata,
                        ),
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

    # -- Read ------------------------------------------------------------------

    async def read(
        self, key: SQLiteKey, *, staged: bool = True
    ) -> SQLiteExecutionRecord | None:
        committed = await asyncio.to_thread(self._read_committed_sync, key)

        if staged:
            staging = self._current.get()
            if staging is not None:
                ops = staging.ops.get(key)
                if ops:
                    committed = self._apply_ops(committed, ops)

        return committed

    def _read_committed_sync(self, key: SQLiteKey) -> SQLiteExecutionRecord | None:
        connection = self._connect()
        try:
            return self._read_value_sync(connection, key)
        finally:
            connection.close()

    def _read_value_sync(
        self, connection: sqlite3.Connection, key: SQLiteKey
    ) -> SQLiteExecutionRecord | None:
        row = connection.execute(
            f'SELECT arguments, status, result, metadata FROM "{self._table_name}" '
            "WHERE session_id = ? AND path = ?",
            key,
        ).fetchone()
        if row is None:
            return None
        return SQLiteExecutionRecord(
            arguments=row[0], status=row[1], result=row[2], metadata=row[3]
        )

    async def iter_records(
        self, session_id: str, prefix: str = "", *, staged: bool = True
    ) -> AsyncIterator[tuple[str, SQLiteExecutionRecord]]:
        """The session's records whose path starts with ``prefix``, in path order.

        Committed rows are pulled a batch at a time rather than materialized, so
        a sweep over a large table costs bounded memory. Staged ops are already
        bounded by the open transaction, so those are held and merged in.

        The merge needs one order for both sides: SQLite's BINARY collation
        compares UTF-8 bytes and Python compares code points, which agree,
        because UTF-8 preserves code point order.
        """
        overlay = self._staged_overlay(session_id, prefix) if staged else {}
        staged_paths = sorted(overlay)
        next_staged = 0

        connection = await asyncio.to_thread(self._connect)
        try:
            cursor = await asyncio.to_thread(
                self._select_range, connection, session_id, prefix
            )
            while True:
                rows = await asyncio.to_thread(cursor.fetchmany, _READ_BATCH_SIZE)
                if not rows:
                    break
                for row in rows:
                    path = row[0]
                    while (
                        next_staged < len(staged_paths)
                        and staged_paths[next_staged] < path
                    ):
                        # Staged but not committed, and it sorts before this row.
                        pending = staged_paths[next_staged]
                        next_staged += 1
                        record = self._apply_ops(None, overlay[pending])
                        if record is not None:
                            yield pending, record

                    committed = SQLiteExecutionRecord(
                        arguments=row[1], status=row[2], result=row[3], metadata=row[4]
                    )
                    if (
                        next_staged < len(staged_paths)
                        and staged_paths[next_staged] == path
                    ):
                        next_staged += 1
                        final = self._apply_ops(committed, overlay[path])
                        if final is None:
                            continue
                        committed = final
                    yield path, committed
        finally:
            connection.close()

        for path in staged_paths[next_staged:]:
            record = self._apply_ops(None, overlay[path])
            if record is not None:
                yield path, record

    def _staged_overlay(
        self, session_id: str, prefix: str
    ) -> dict[str, list[_StagedOp]]:
        staging = self._current.get()
        if staging is None:
            return {}
        return {
            path: ops
            for (session, path), ops in staging.ops.items()
            if session == session_id and path.startswith(prefix)
        }

    def _select_range(
        self, connection: sqlite3.Connection, session_id: str, prefix: str
    ) -> sqlite3.Cursor:
        columns = (
            "SELECT path, arguments, status, result, metadata "
            f'FROM "{self._table_name}"'
        )
        if not prefix:
            return connection.execute(
                f"{columns} WHERE session_id = ? ORDER BY path", (session_id,)
            )
        # A range over the primary key, not substr(): the latter is not sargable
        # and scans the whole table.
        return connection.execute(
            f"{columns} WHERE session_id = ? AND path >= ? AND path < ? ORDER BY path",
            (session_id, prefix, _prefix_upper_bound(prefix)),
        )

    # -- Application version ---------------------------------------------------

    async def claim_session(self, session_id: str, app_version: str) -> str:
        """Records ``app_version`` for a session that carries none; returns the winner."""
        async with self._write_lock:
            return await asyncio.to_thread(
                self._claim_session_sync, session_id, app_version
            )

    def _claim_session_sync(self, session_id: str, app_version: str) -> str:
        connection = self._connect()
        in_transaction = False
        try:
            # One BEGIN IMMEDIATE around the insert and the read: a concurrent
            # claim either waits for this commit and then reads the winner, or
            # takes the write lock first and makes this one read its version.
            connection.execute("BEGIN IMMEDIATE")
            in_transaction = True
            connection.execute(
                f'INSERT INTO "{self._sessions_table_name}" '
                "(session_id, app_version) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO NOTHING",
                (session_id, app_version),
            )
            recorded = connection.execute(
                f'SELECT app_version FROM "{self._sessions_table_name}" '
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            connection.execute("COMMIT")
            in_transaction = False
            return recorded
        except BaseException:
            if in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
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
