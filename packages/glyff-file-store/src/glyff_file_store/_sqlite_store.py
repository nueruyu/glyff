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
    SessionStore,
    Transaction,
)
from glyff.serialization import JsonSerializer
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


class _SQLiteStagedRow:
    __slots__ = (
        "execution_id",
        "status",
        "result_json",
        "error",
        "on_conflict_update",
    )

    def __init__(
        self,
        execution_id: ExecutionId,
        status: ExecutionStatus,
        result_json: str | None,
        error: str | None,
        *,
        on_conflict_update: bool,
    ) -> None:
        self.execution_id = execution_id
        self.status = status
        self.result_json = result_json
        self.error = error
        self.on_conflict_update = on_conflict_update


class _SQLiteStagingBuffer:
    __slots__ = ("writes", "delete_keys")

    def __init__(self) -> None:
        self.writes: dict[str, _SQLiteStagedRow] = {}
        self.delete_keys: set[str] = set()

    def clear(self) -> None:
        self.writes.clear()
        self.delete_keys.clear()


class _SQLiteTransaction(Transaction):
    def __init__(self, store: SQLiteSessionStore, staging: _SQLiteStagingBuffer):
        self._store = store
        self._staging = staging
        self._lock = asyncio.Lock()
        self._token: contextvars.Token | None = None
        self._closed = False

    async def commit(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                await self._store._commit_staged(self._staging)
            finally:
                self._store._end_transaction(self._token)

    async def rollback(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._staging.clear()
            finally:
                self._store._end_transaction(self._token)


class _SQLiteExecution(Execution):
    def __init__(
        self,
        store: SQLiteSessionStore,
        execution_id: ExecutionId,
        serializer: JsonSerializer,
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
    """A SQLite-backed SessionStore for durable local persistence."""

    def __init__(
        self,
        database_path: str | Path,
        serializer: JsonSerializer,
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
        self._write_lock = asyncio.Lock()
        self._current_tx: contextvars.ContextVar[_SQLiteTransaction | None] = (
            contextvars.ContextVar("sqlite_current_tx", default=None)
        )
        self._initialize_database()

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

    async def _read(self, fn: _ReadFn) -> Any:
        return await asyncio.to_thread(self._read_fresh, fn)

    def _read_fresh(self, fn: _ReadFn) -> Any:
        connection = self._connect()
        try:
            return fn(connection)
        finally:
            connection.close()

    def _require_staging(self) -> _SQLiteStagingBuffer:
        tx = self._current_tx.get()
        if tx is None:
            raise RuntimeError(
                "SQLiteSessionStore write attempted outside a transaction."
            )
        if tx._closed:
            raise RuntimeError(
                "SQLiteSessionStore write attempted on a closed transaction."
            )
        return tx._staging

    async def _commit_staged(self, staging: _SQLiteStagingBuffer) -> None:
        if not staging.writes and not staging.delete_keys:
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
            for key in staging.delete_keys:
                connection.execute("DELETE FROM executions WHERE key = ?", (key,))
            for row in staging.writes.values():
                key = self._id_to_key(row.execution_id)
                if key in staging.delete_keys:
                    continue
                self._write_row_sync(
                    connection,
                    row.execution_id,
                    row.status,
                    row.result_json,
                    row.error,
                    on_conflict_update=row.on_conflict_update,
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
        self._stage_row(
            execution_id,
            status,
            result_json,
            error,
            on_conflict_update=True,
        )

    def _stage_row(
        self,
        execution_id: ExecutionId,
        status: ExecutionStatus,
        result_json: str | None,
        error: str | None,
        *,
        on_conflict_update: bool,
    ) -> None:
        staging = self._require_staging()
        key = self._id_to_key(execution_id)
        staging.writes[key] = _SQLiteStagedRow(
            execution_id,
            status,
            result_json,
            error,
            on_conflict_update=on_conflict_update,
        )
        staging.delete_keys.discard(key)

    async def begin_transaction(self) -> Transaction:
        transaction = _SQLiteTransaction(self, _SQLiteStagingBuffer())
        transaction._token = self._current_tx.set(transaction)
        return transaction

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        if await self.get_execution_record(execution_id, type(None)) is None:
            self._stage_row(
                execution_id,
                ExecutionStatus.STARTED,
                None,
                None,
                on_conflict_update=False,
            )
        return _SQLiteExecution(self, execution_id, self._serializer)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        key = self._id_to_key(execution_id)
        tx = self._current_tx.get()
        if tx is not None and not tx._closed:
            if key in tx._staging.delete_keys:
                return None
            staged = tx._staging.writes.get(key)
            if staged is not None:
                return await self._row_to_record(
                    staged.status,
                    staged.result_json,
                    staged.error,
                    return_type,
                )

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
        return await self._row_to_record(status, result_json, error, return_type)

    async def _row_to_record(
        self,
        status: ExecutionStatus,
        result_json: str | None,
        error: str | None,
        return_type: type,
    ) -> ExecutionRecord:
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
        by_key = {key: json.loads(call_stack) for key, call_stack in rows}
        tx = self._current_tx.get()
        if tx is not None and not tx._closed:
            for key in tx._staging.delete_keys:
                by_key.pop(key, None)
            for key, row in tx._staging.writes.items():
                if key.startswith(prefix):
                    by_key[key] = self._id_to_callstack(row.execution_id)
        return [self._callstack_to_id(call_stack) for call_stack in by_key.values()]

    async def delete_executions(self, execution_ids: Iterable[ExecutionId]) -> None:
        keys = [self._id_to_key(eid) for eid in execution_ids]
        if not keys:
            return
        staging = self._require_staging()
        for key in keys:
            staging.delete_keys.add(key)
            staging.writes.pop(key, None)
