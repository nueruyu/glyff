from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glyff import Execution, ExecutionId
from glyff.exceptions import MigrationError, StoreFormatVersionError
from glyff.migration import (
    MigrationReport,
    SessionMetadata,
    SessionMigrationResult,
    StoredSession,
)
from glyff.serialization.constants import JSON_SEPARATORS
from glyff.store.aggregate_codec import execution_from_dict, execution_to_dict
from glyff.store.staging import (
    DeleteExecution,
    ExecutionKey,
    ExecutionMutation,
)
from glyff.store.utils import execution_id_to_path, path_to_execution_id

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
    """One row of the executions table, as columns of JSON text."""

    arguments: str
    status: str
    result: str | None
    metadata: str

    @classmethod
    def from_execution(cls, execution: Execution) -> SQLiteExecutionRecord:
        stored = execution_to_dict(execution)
        return cls(
            arguments=stored["arguments"],
            status=stored["status"],
            result=_json_text(stored["result"])
            if execution.result is not None
            else None,
            metadata=_json_text(stored["metadata"]),
        )

    def to_execution(self, execution_id: ExecutionId) -> Execution:
        return execution_from_dict(
            execution_id,
            {
                "arguments": self.arguments,
                "status": self.status,
                "result": json.loads(self.result) if self.result is not None else None,
                "metadata": json.loads(self.metadata),
            },
        )


def _json_text(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=JSON_SEPARATORS
    )


_VALID_SYNCHRONOUS_VALUES = {"OFF", "NORMAL", "FULL", "EXTRA"}

_READ_BATCH_SIZE = 256


def _prefix_upper_bound(prefix: str) -> str:
    """The least string ordering above every string that starts with ``prefix``.

    SQLite compares TEXT by UTF-8 bytes, which order the same as code points, so
    incrementing the last one bounds the range.
    """
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


class SQLiteClient:
    """Committed rows of the execution tables, and the batch that replaces them.

    Each execution is a row of ``<table_prefix>_executions`` (default prefix
    ``glyff``) keyed by ``(session_id, path)``. A sibling
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

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """A configured connection, closed however the block ends."""
        with closing(self._connect()) as connection:
            yield connection

    @contextmanager
    def _immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE``: committed if the block returns, rolled back
        if anything in it — or the commit itself — raises.

        A failed rollback is swallowed so it cannot replace the failure that
        caused it. A failed ``BEGIN`` leaves nothing to roll back.
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                with suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise

    # -- Schema initialization -------------------------------------------------

    def _initialize_schema_sync(self) -> None:
        with self._immediate_transaction() as connection:
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

    # -- Commit ----------------------------------------------------------------

    async def commit_mutations(
        self, mutations: Mapping[ExecutionKey, ExecutionMutation]
    ) -> None:
        if not mutations:
            return

        async with self._write_lock:
            await asyncio.to_thread(self._commit_mutations_sync, mutations)

    def _commit_mutations_sync(
        self, mutations: Mapping[ExecutionKey, ExecutionMutation]
    ) -> None:
        with self._immediate_transaction() as connection:
            for key, mutation in mutations.items():
                self._apply_mutation(connection, key, mutation)

    def _apply_mutation(
        self,
        connection: sqlite3.Connection,
        key: ExecutionKey,
        mutation: ExecutionMutation,
    ) -> None:
        if isinstance(mutation, DeleteExecution):
            connection.execute(
                f'DELETE FROM "{self._table_name}" WHERE session_id = ? AND path = ?',
                (key.session_id.value, execution_id_to_path(key.execution_id)),
            )
            return

        self._upsert_execution(
            connection, key.session_id.value, mutation.snapshot.to_execution()
        )

    def _upsert_execution(
        self, connection: sqlite3.Connection, session_id: str, execution: Execution
    ) -> None:
        record = SQLiteExecutionRecord.from_execution(execution)
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
                execution_id_to_path(execution.id),
                record.arguments,
                record.status,
                record.result,
                record.metadata,
            ),
        )

    # -- Read ------------------------------------------------------------------

    async def read_committed(
        self, session_id: str, path: str
    ) -> SQLiteExecutionRecord | None:
        return await asyncio.to_thread(self._read_committed_sync, session_id, path)

    def _read_committed_sync(
        self, session_id: str, path: str
    ) -> SQLiteExecutionRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                f'SELECT arguments, status, result, metadata FROM "{self._table_name}" '
                "WHERE session_id = ? AND path = ?",
                (session_id, path),
            ).fetchone()
            if row is None:
                return None
            return SQLiteExecutionRecord(
                arguments=row[0], status=row[1], result=row[2], metadata=row[3]
            )

    async def iter_committed(
        self, session_id: str, prefix: str = ""
    ) -> AsyncIterator[tuple[str, SQLiteExecutionRecord]]:
        """The session's committed rows whose path starts with ``prefix``, in
        path order.

        Rows are pulled a batch at a time rather than materialized, so a sweep
        over a large table costs bounded memory.
        """
        # Connecting blocks, so it stays on a worker thread rather than going
        # through _connection(); closing() gives the same guarantee around the
        # one connection and cursor this iteration holds.
        connection = await asyncio.to_thread(self._connect)
        with closing(connection):
            cursor = await asyncio.to_thread(
                self._select_range, connection, session_id, prefix
            )
            while True:
                rows = await asyncio.to_thread(cursor.fetchmany, _READ_BATCH_SIZE)
                if not rows:
                    break
                for row in rows:
                    yield (
                        row[0],
                        SQLiteExecutionRecord(
                            arguments=row[1],
                            status=row[2],
                            result=row[3],
                            metadata=row[4],
                        ),
                    )

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
        # The insert and the read share one BEGIN IMMEDIATE: a concurrent claim
        # either waits for this commit and then reads the winner, or takes the
        # write lock first and makes this one read its version.
        with self._immediate_transaction() as connection:
            connection.execute(
                f'INSERT INTO "{self._sessions_table_name}" '
                "(session_id, app_version) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO NOTHING",
                (session_id, app_version),
            )
            return connection.execute(
                f'SELECT app_version FROM "{self._sessions_table_name}" '
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]

    # -- Session migration -----------------------------------------------------

    async def migrate_session(
        self,
        session_id: str,
        migrate: Callable[[StoredSession], SessionMigrationResult],
    ) -> MigrationReport:
        """Replaces one session's metadata and executions in a single write.

        ``migrate`` runs with the session held, so it must not do I/O of its own.
        """
        async with self._write_lock:
            return await asyncio.to_thread(
                self._migrate_session_sync, session_id, migrate
            )

    def _migrate_session_sync(
        self,
        session_id: str,
        migrate: Callable[[StoredSession], SessionMigrationResult],
    ) -> MigrationReport:
        # SQLite has no row locks, so the exclusion is the transaction itself:
        # BEGIN IMMEDIATE takes the write lock before the first read and holds it
        # past the last write, which makes every other writer wait rather than
        # act on the state this is replacing.
        with self._immediate_transaction() as connection:
            source = self._read_session(connection, session_id)
            result = migrate(source)

            connection.execute(
                f'DELETE FROM "{self._table_name}" WHERE session_id = ?', (session_id,)
            )
            for execution in result.session.executions:
                self._upsert_execution(connection, session_id, execution)

            connection.execute(
                f'INSERT INTO "{self._sessions_table_name}" '
                "(session_id, app_version) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET app_version = excluded.app_version",
                (session_id, result.session.metadata.app_version),
            )
            return result.report

    def _read_session(
        self, connection: sqlite3.Connection, session_id: str
    ) -> StoredSession:
        row = connection.execute(
            f'SELECT app_version FROM "{self._sessions_table_name}" '
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise MigrationError(
                f"Session {session_id!r} carries no application version in "
                f"{self._database_path}, so there is no version to migrate from."
            )

        rows = connection.execute(
            "SELECT path, arguments, status, result, metadata "
            f'FROM "{self._table_name}" WHERE session_id = ? ORDER BY path',
            (session_id,),
        ).fetchall()
        return StoredSession(
            metadata=SessionMetadata(app_version=row[0]),
            # Path order is ancestor-first: a parent's path is a prefix of its
            # children's, and a prefix sorts before what extends it.
            executions=tuple(
                SQLiteExecutionRecord(
                    arguments=record[1],
                    status=record[2],
                    result=record[3],
                    metadata=record[4],
                ).to_execution(path_to_execution_id(record[0]))
                for record in rows
            ),
        )

    # -- Direct SQL access (for initialization / inspection) -------------------

    async def read_sql(self, sql: str, *params: Any) -> list[tuple]:
        return await asyncio.to_thread(self._read_sql_sync, sql, params)

    def _read_sql_sync(self, sql: str, params: tuple[Any, ...]) -> list[tuple]:
        with self._connection() as connection:
            return list(connection.execute(sql, params))

    async def execute(self, sql: str, *params: Any) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._execute_sync, sql, params)

    def _execute_sync(self, sql: str, params: tuple[Any, ...]) -> None:
        with self._immediate_transaction() as connection:
            connection.execute(sql, params)
