from __future__ import annotations

import asyncio
import contextvars
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

SQLiteRead = Callable[[sqlite3.Connection], T]
SQLiteWrite = Callable[[sqlite3.Connection], object]

_VALID_SYNCHRONOUS_VALUES = {"OFF", "NORMAL", "FULL", "EXTRA"}


class _SQLiteStagingBuffer:
    __slots__ = ("writes",)

    def __init__(self) -> None:
        self.writes: list[SQLiteWrite] = []

    def clear(self) -> None:
        self.writes.clear()


class SQLiteClient:
    """SQLite transaction coordinator with transaction-local staging.

    This class does not know about glyff executions, metadata, blobs, or any
    domain schema. It only stages SQL write callbacks and commits them in one
    physical SQLite transaction.
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
            contextvars.ContextVar("sqlite_current_staging", default=None)
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

    async def read(self, fn: SQLiteRead[T]) -> T:
        return await asyncio.to_thread(self._read_sync, fn)

    def _read_sync(self, fn: SQLiteRead[T]) -> T:
        connection = self._connect()
        try:
            return fn(connection)
        finally:
            connection.close()

    async def apply(self, fn: SQLiteWrite) -> None:
        """Apply an immediate SQL write in its own physical transaction.

        Intended for schema initialization or explicit out-of-band setup, not
        for execution/body transaction work.
        """
        async with self._write_lock:
            await asyncio.to_thread(self._apply_sync, fn)

    def _apply_sync(self, fn: SQLiteWrite) -> None:
        connection = self._connect()
        in_transaction = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            in_transaction = True
            fn(connection)
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

    # -- Staging lifecycle ----------------------------------------------------

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

    def stage(self, fn: SQLiteWrite) -> None:
        staging = self._require_staging()
        staging.writes.append(fn)

    def clear_staged(self) -> None:
        self._require_staging().clear()

    async def commit_staged(self) -> None:
        staging = self._require_staging()
        await self._commit_staged(staging)

    async def _commit_staged(self, staging: _SQLiteStagingBuffer) -> None:
        if not staging.writes:
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

            for write in staging.writes:
                write(connection)

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
