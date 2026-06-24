from __future__ import annotations

import asyncio
import contextvars
import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from glyff import (
    Execution,
    ExecutionId,
    ExecutionRecord,
    ExecutionStatus,
    Serializer,
    SessionStore,
    Transaction,
)
from glyff.serialization.constants import DEFAULT_ENCODING, JSON_SEPARATORS
from glyff.store.utils import execution_id_to_path, path_to_execution_id

_STATUS_TO_EVENT_TYPE = {
    ExecutionStatus.STARTED: "start",
    ExecutionStatus.COMPLETED: "complete",
    ExecutionStatus.FAILED: "fail",
}
_EVENT_TYPE_TO_STATUS = {v: k for k, v in _STATUS_TO_EVENT_TYPE.items()}
_VALID_SYNCHRONOUS_VALUES = {"OFF", "NORMAL", "FULL", "EXTRA"}

_ReadFn = Callable[[sqlite3.Connection], Any]
_WriteFn = Callable[[sqlite3.Connection], None]


class _SQLiteTransaction(Transaction):
    """A native SQLite transaction.

    Staging is the database's own uncommitted state: each operation runs INSERT
    / DELETE directly on this transaction's connection (inside ``BEGIN``), and
    ``commit`` / ``rollback`` are native ``COMMIT`` / ``ROLLBACK``. Multiple
    stagers (the store plus any external code holding this transaction) write to
    the same connection and are committed atomically together; ``_lock``
    serializes concurrent access to that single connection.
    """

    def __init__(self, store: SQLiteSessionStore, connection: sqlite3.Connection):
        self._store = store
        self._connection = connection
        self._lock = asyncio.Lock()
        self._token: contextvars.Token | None = None
        self._closed = False

    async def commit(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                await asyncio.to_thread(self._commit_sync)
            finally:
                self._store._end_transaction(self._token)

    async def rollback(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                await asyncio.to_thread(self._rollback_sync)
            finally:
                self._store._end_transaction(self._token)

    def _commit_sync(self) -> None:
        try:
            self._connection.execute("COMMIT")
        finally:
            self._connection.close()

    def _rollback_sync(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        finally:
            self._connection.close()


class _SQLiteExecution(Execution):
    def __init__(
        self,
        store: SQLiteSessionStore,
        execution_id: ExecutionId,
        serializer: Serializer,
    ):
        self._store = store
        self._execution_id = execution_id
        self._serializer = serializer

    async def complete(self, value: object, return_type: type) -> None:
        serialized_bytes = await self._serializer.serialize(value, return_type)
        result_json = serialized_bytes.decode(DEFAULT_ENCODING)
        await self._store._upsert(
            self._execution_id, ExecutionStatus.COMPLETED, result_json=result_json
        )

    async def fail(self, error: str) -> None:
        await self._store._upsert(
            self._execution_id, ExecutionStatus.FAILED, error=error
        )


class SQLiteSessionStore(SessionStore):
    """
    A SQLite-backed SessionStore for durable local persistence.

    The store keeps one row per execution id and uses native SQLite transactions
    (no in-memory staging): ``begin_transaction`` opens a connection in ``BEGIN
    IMMEDIATE`` and each ``start_execution`` / ``Execution.complete`` / ``fail`` /
    ``delete_executions`` runs directly on it, becoming durable on ``commit``.
    Multiple stores or application code sharing one transaction commit together
    atomically.

    It is the durable, parallel-safe backend; JsonFileSessionStore remains the
    human-readable debug format.
    """

    def __init__(
        self,
        database_path: str | Path,
        serializer: Serializer,
        *,
        busy_timeout_ms: int = 30_000,
        synchronous: str = "FULL",
    ):
        synchronous = synchronous.upper()
        if synchronous not in _VALID_SYNCHRONOUS_VALUES:
            valid = ", ".join(sorted(_VALID_SYNCHRONOUS_VALUES))
            raise ValueError(f"synchronous must be one of: {valid}")

        self._database_path = Path(database_path)
        self._serializer = serializer
        self._busy_timeout_ms = busy_timeout_ms
        self._synchronous = synchronous
        # Serializes write transactions within this process: only one
        # BEGIN IMMEDIATE runs at a time, so concurrent transactions never
        # block worker threads waiting on the SQLite write lock (which could
        # exhaust the thread pool). Cross-process contention is handled by
        # busy_timeout. Released by the transaction on commit/rollback.
        self._write_lock = asyncio.Lock()
        # The transaction active in the current asyncio task, if any. Parallel
        # gather branches run in copied contexts, so each sees its own.
        self._current_tx: contextvars.ContextVar[_SQLiteTransaction | None] = (
            contextvars.ContextVar("sqlite_current_tx", default=None)
        )
        self._initialize_database()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

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

    def _open_tx_connection(self) -> sqlite3.Connection:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            return connection
        except BaseException:
            connection.close()
            raise

    def _initialize_database(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    key TEXT PRIMARY KEY,
                    call_stack_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('start', 'complete', 'fail')
                    ),
                    result_json TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
        finally:
            connection.close()

    def _end_transaction(self, token: contextvars.Token | None) -> None:
        if token is not None:
            try:
                self._current_tx.reset(token)
            except (ValueError, LookupError):
                pass
        if self._write_lock.locked():
            self._write_lock.release()

    # ------------------------------------------------------------------
    # Read / write routing
    # ------------------------------------------------------------------

    async def _read(self, fn: _ReadFn) -> Any:
        """Run a read against the current transaction's connection (so it sees
        this transaction's uncommitted writes), or a fresh connection when there
        is no active transaction (committed state only)."""
        tx = self._current_tx.get()
        if tx is not None:
            async with tx._lock:
                return await asyncio.to_thread(fn, tx._connection)
        return await asyncio.to_thread(self._read_fresh, fn)

    def _read_fresh(self, fn: _ReadFn) -> Any:
        connection = self._connect()
        try:
            return fn(connection)
        finally:
            connection.close()

    async def _write(self, fn: _WriteFn) -> None:
        """Run a write on the current transaction's connection. Writes only
        happen inside a transaction (the executor opens one per event)."""
        tx = self._current_tx.get()
        if tx is None:
            raise RuntimeError(
                "SQLiteSessionStore write attempted outside a transaction."
            )
        async with tx._lock:
            await asyncio.to_thread(fn, tx._connection)

    # ------------------------------------------------------------------
    # Id / call-stack helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _callstack_to_id(call_stack: list[str]) -> ExecutionId:
        return path_to_execution_id("/".join(call_stack))

    def _id_to_callstack(self, execution_id: ExecutionId) -> list[str]:
        return execution_id_to_path(execution_id).split("/")

    def _id_to_key(self, execution_id: ExecutionId) -> str:
        return execution_id_to_path(execution_id)

    def _write_row_sync(
        self,
        connection: sqlite3.Connection,
        execution_id: ExecutionId,
        status: ExecutionStatus,
        result_json: str | None,
        error: str | None,
        *,
        on_conflict_update: bool,
    ) -> None:
        key = self._id_to_key(execution_id)
        call_stack = self._id_to_callstack(execution_id)
        conflict = (
            """
            ON CONFLICT(key) DO UPDATE SET
                call_stack_json = excluded.call_stack_json,
                status = excluded.status,
                result_json = excluded.result_json,
                error = excluded.error,
                updated_at = excluded.updated_at
            """
            if on_conflict_update
            else "ON CONFLICT(key) DO NOTHING"
        )
        connection.execute(
            f"""
            INSERT INTO executions (
                key, call_stack_json, status, result_json, error, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            {conflict}
            """,
            (
                key,
                json.dumps(call_stack, separators=JSON_SEPARATORS),
                _STATUS_TO_EVENT_TYPE[status],
                result_json,
                error,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    async def _upsert(
        self,
        execution_id: ExecutionId,
        status: ExecutionStatus,
        *,
        result_json: str | None = None,
        error: str | None = None,
    ) -> None:
        def op(connection: sqlite3.Connection) -> None:
            self._write_row_sync(
                connection,
                execution_id,
                status,
                result_json,
                error,
                on_conflict_update=True,
            )

        await self._write(op)

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    async def begin_transaction(self) -> Transaction:
        await self._write_lock.acquire()
        try:
            connection = await asyncio.to_thread(self._open_tx_connection)
        except BaseException:
            self._write_lock.release()
            raise
        transaction = _SQLiteTransaction(self, connection)
        transaction._token = self._current_tx.set(transaction)
        return transaction

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        def op(connection: sqlite3.Connection) -> None:
            # Insert the STARTED row only if no record exists yet, so a prior
            # COMPLETED/FAILED/STARTED record is never clobbered on replay.
            self._write_row_sync(
                connection,
                execution_id,
                ExecutionStatus.STARTED,
                None,
                None,
                on_conflict_update=False,
            )

        await self._write(op)
        return _SQLiteExecution(self, execution_id, self._serializer)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        key = self._id_to_key(execution_id)

        def read(connection: sqlite3.Connection):
            return connection.execute(
                "SELECT status, result_json, error FROM executions WHERE key = ?",
                (key,),
            ).fetchone()

        row = await self._read(read)
        if row is None:
            return None

        status_value, result_json, error = row
        status = _EVENT_TYPE_TO_STATUS[status_value]
        result: Any | None = None
        if status == ExecutionStatus.COMPLETED and result_json is not None:
            result = await self._serializer.deserialize(
                result_json.encode(DEFAULT_ENCODING), return_type
            )
        elif status == ExecutionStatus.FAILED:
            error = error or ""
        return ExecutionRecord(status=status, result=result, error=error)

    async def get_descendants(self, execution_id: ExecutionId) -> list[ExecutionId]:
        prefix = self._id_to_key(execution_id) + "/"
        pattern = self._escape_like(prefix) + "%"

        def read(connection: sqlite3.Connection) -> list[tuple[str, str]]:
            return list(
                connection.execute(
                    """
                    SELECT key, call_stack_json FROM executions
                    WHERE key LIKE ? ESCAPE '\\'
                    ORDER BY key
                    """,
                    (pattern,),
                )
            )

        rows = await self._read(read)
        return [self._callstack_to_id(json.loads(call_stack)) for _key, call_stack in rows]

    async def delete_executions(self, execution_ids: Iterable[ExecutionId]) -> None:
        keys = [self._id_to_key(eid) for eid in execution_ids]
        if not keys:
            return

        def op(connection: sqlite3.Connection) -> None:
            connection.executemany(
                "DELETE FROM executions WHERE key = ?", [(key,) for key in keys]
            )

        await self._write(op)
