from __future__ import annotations

import asyncio
import contextvars
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SQLiteUpdate = Callable[[bytes | None], bytes | None]


@dataclass(frozen=True)
class _Write:
    value: bytes


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
        self.ops: dict[tuple[str, str], list[_StagedOp]] = {}

    def clear(self) -> None:
        self.ops.clear()


class SQLiteClient:
    """A generic SQLite-backed transactional key/value store.

    Each record is identified by ``(namespace, key)`` and stored as a blob
    in the ``records`` table. Operations are staged per-transaction and
    committed atomically.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 30_000,
        synchronous: str = "FULL",
    ) -> None:
        synchronous = synchronous.upper()
        if synchronous not in _VALID_SYNCHRONOUS_VALUES:
            valid = ", ".join(sorted(_VALID_SYNCHRONOUS_VALUES))
            raise ValueError(f"synchronous must be one of: {valid}")

        self._database_path = Path(database_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._synchronous = synchronous
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
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value BLOB NOT NULL,
                    PRIMARY KEY (namespace, key)
                )
                """
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

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
            raise RuntimeError(
                "SQLiteClient write attempted outside a transaction."
            )
        return staging

    def _require_current_staging(self, expected: _SQLiteStagingBuffer) -> None:
        if self._current.get() is not expected:
            raise RuntimeError("Transaction closed out of order.")

    # -- Staging API -----------------------------------------------------------

    def stage_write(self, namespace: str, key: str, value: bytes) -> None:
        staging = self._require_staging()
        staging.ops.setdefault((namespace, key), []).append(_Write(value))

    def stage_delete(self, namespace: str, key: str) -> None:
        staging = self._require_staging()
        staging.ops.setdefault((namespace, key), []).append(_Delete())

    def stage_update(
        self, namespace: str, key: str, fn: SQLiteUpdate
    ) -> None:
        staging = self._require_staging()
        staging.ops.setdefault((namespace, key), []).append(_Update(fn))

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

        connection.execute("BEGIN IMMEDIATE")
        try:
            for (namespace, key), ops in staging.ops.items():
                current = self._read_value_sync(connection, namespace, key)
                result = self._apply_ops(current, ops)

                if result is None:
                    connection.execute(
                        "DELETE FROM records WHERE namespace = ? AND key = ?",
                        (namespace, key),
                    )
                else:
                    connection.execute(
                        """INSERT INTO records (namespace, key, value)
                           VALUES (?, ?, ?)
                           ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value""",
                        (namespace, key, result),
                    )

            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _apply_ops(data: bytes | None, ops: list[_StagedOp]) -> bytes | None:
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

    # -- Read / list_keys ------------------------------------------------------

    async def read(
        self, namespace: str, key: str, *, staged: bool = True
    ) -> bytes | None:
        committed = await asyncio.to_thread(
            self._read_committed_sync, namespace, key
        )

        if staged:
            staging = self._current.get()
            if staging is not None:
                ops = staging.ops.get((namespace, key))
                if ops:
                    committed = self._apply_ops(committed, ops)

        return committed

    def _read_committed_sync(
        self, namespace: str, key: str
    ) -> bytes | None:
        connection = self._connect()
        try:
            return self._read_value_sync(connection, namespace, key)
        finally:
            connection.close()

    @staticmethod
    def _read_value_sync(
        connection: sqlite3.Connection, namespace: str, key: str
    ) -> bytes | None:
        row = connection.execute(
            "SELECT value FROM records WHERE namespace = ? AND key = ?",
            (namespace, key),
        ).fetchone()
        return row[0] if row else None

    async def list_keys(
        self, namespace: str, prefix: str = "", *, staged: bool = True
    ) -> set[str]:
        committed = await asyncio.to_thread(
            self._list_committed_keys_sync, namespace, prefix
        )

        if staged:
            staging = self._current.get()
            if staging is not None:
                for (ns, key), ops in staging.ops.items():
                    if ns != namespace:
                        continue
                    if not key.startswith(prefix):
                        continue
                    final = self._apply_ops(
                        self._read_committed_sync(namespace, key),
                        ops,
                    )
                    if final is None:
                        committed.discard(key)
                    else:
                        committed.add(key)

        return committed

    def _list_committed_keys_sync(
        self, namespace: str, prefix: str = ""
    ) -> set[str]:
        connection = self._connect()
        try:
            pattern = prefix + "%"
            rows = connection.execute(
                "SELECT key FROM records WHERE namespace = ? AND key LIKE ?",
                (namespace, pattern),
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

    def _execute_sync(
        self, sql: str, params: tuple[Any, ...]
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(sql, params)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
