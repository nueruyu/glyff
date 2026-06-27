from __future__ import annotations

import asyncio
import contextvars
import json
import sqlite3
from collections.abc import Iterable
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

from ._sqlite_client import SQLiteClient

_STATUS_TO_EVENT_TYPE = {
    ExecutionStatus.STARTED: "start",
    ExecutionStatus.COMPLETED: "complete",
    ExecutionStatus.FAILED: "fail",
}
_EVENT_TYPE_TO_STATUS = {v: k for k, v in _STATUS_TO_EVENT_TYPE.items()}


class _StagedExecutionRow:
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


class _ExecutionStagingBuffer:
    __slots__ = ("writes", "delete_keys")

    def __init__(self) -> None:
        self.writes: dict[str, _StagedExecutionRow] = {}
        self.delete_keys: set[str] = set()

    def clear(self) -> None:
        self.writes.clear()
        self.delete_keys.clear()


class _SQLiteSessionTransaction(Transaction):
    def __init__(self, store: SQLiteSessionStore):
        self._store = store
        self._closed = False
        self._lock = asyncio.Lock()
        self._client_token: contextvars.Token | None = None
        self._client_staging: Any = None
        self._store_token: contextvars.Token | None = None
        self._store_staging: _ExecutionStagingBuffer | None = None

    async def begin(self) -> _SQLiteSessionTransaction:
        self._client_token, self._client_staging = self._store._client.begin_staging()
        self._store_staging = _ExecutionStagingBuffer()
        self._store_token = self._store._current.set(self._store_staging)
        return self

    def _ensure_current(self) -> _ExecutionStagingBuffer:
        if self._store_staging is None:
            raise RuntimeError("Transaction is not started.")
        if self._store._current.get() is not self._store_staging:
            raise RuntimeError("Transaction closed out of order.")
        self._store._client._require_current_staging(self._client_staging)
        return self._store_staging

    async def commit(self) -> None:
        async with self._lock:
            if self._closed:
                return
            staging = self._ensure_current()
            self._closed = True
            try:
                self._store._flush_execution_staging_to_client(staging)
                await self._store._client.commit_staged()
            finally:
                if self._store_token is not None:
                    self._store._current.reset(self._store_token)
                if self._client_token is not None:
                    self._store._client.end_staging(self._client_token)

    async def rollback(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._ensure_current()
            self._closed = True
            try:
                if self._store_staging is not None:
                    self._store_staging.clear()
                self._store._client.clear_staged()
            finally:
                if self._store_token is not None:
                    self._store._current.reset(self._store_token)
                if self._client_token is not None:
                    self._store._client.end_staging(self._client_token)


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
        self._store._stage_row(
            self._execution_id,
            ExecutionStatus.COMPLETED,
            result_json,
            None,
            on_conflict_update=True,
        )

    async def fail(self, error: str) -> None:
        self._store._stage_row(
            self._execution_id,
            ExecutionStatus.FAILED,
            None,
            error,
            on_conflict_update=True,
        )


class SQLiteSessionStore(SessionStore):
    """A SQLite-backed SessionStore for durable local persistence."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        serializer: JsonSerializer | None = None,
        *,
        client: SQLiteClient | None = None,
        busy_timeout_ms: int = 30_000,
        synchronous: str = "FULL",
    ):
        if serializer is None:
            raise TypeError("serializer is required.")

        if client is None:
            if database_path is None:
                raise TypeError("database_path or client is required.")
            client = SQLiteClient(
                database_path,
                busy_timeout_ms=busy_timeout_ms,
                synchronous=synchronous,
            )
        elif database_path is not None:
            raise TypeError("Pass either database_path or client, not both.")

        self._client = client
        self._serializer = serializer
        self._current: contextvars.ContextVar[_ExecutionStagingBuffer | None] = (
            contextvars.ContextVar("sqlite_execution_staging", default=None)
        )

        self._initialize_schema()

    @property
    def client(self) -> SQLiteClient:
        return self._client

    # -- Schema initialization -------------------------------------------------

    def _initialize_schema(self) -> None:
        def create_schema(connection: sqlite3.Connection) -> None:
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

        self._client._apply_sync(create_schema)

    # -- Staging context management --------------------------------------------

    def _require_staging(self) -> _ExecutionStagingBuffer:
        staging = self._current.get()
        if staging is None:
            raise RuntimeError(
                "SQLiteSessionStore write attempted outside a transaction."
            )
        return staging

    # -- Execution row staging -------------------------------------------------

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
        staging.writes[key] = _StagedExecutionRow(
            execution_id,
            status,
            result_json,
            error,
            on_conflict_update=on_conflict_update,
        )
        staging.delete_keys.discard(key)

    def _stage_delete_executions(self, execution_ids: Iterable[ExecutionId]) -> None:
        staging = self._require_staging()
        for execution_id in execution_ids:
            key = self._id_to_key(execution_id)
            staging.delete_keys.add(key)
            staging.writes.pop(key, None)

    # -- Flush staging to client -----------------------------------------------

    def _flush_execution_staging_to_client(
        self,
        staging: _ExecutionStagingBuffer,
    ) -> None:
        if not staging.writes and not staging.delete_keys:
            return

        delete_keys = set(staging.delete_keys)
        rows = list(staging.writes.values())
        store = self

        def write(connection: sqlite3.Connection) -> None:
            for key in delete_keys:
                connection.execute("DELETE FROM executions WHERE key = ?", (key,))

            for row in rows:
                key = store._id_to_key(row.execution_id)
                if key in delete_keys:
                    continue
                store._write_row_sync(
                    connection,
                    row.execution_id,
                    row.status,
                    row.result_json,
                    row.error,
                    on_conflict_update=row.on_conflict_update,
                )

        self._client.stage(write)
        staging.clear()

    # -- Helpers ---------------------------------------------------------------

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _callstack_to_id(call_stack: list[str]) -> ExecutionId:
        return path_to_execution_id("/".join(call_stack))

    @staticmethod
    def _id_to_callstack(execution_id: ExecutionId) -> list[str]:
        return execution_id_to_path(execution_id).split("/")

    @staticmethod
    def _id_to_key(execution_id: ExecutionId) -> str:
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

    # -- SessionStore API ------------------------------------------------------

    async def begin_transaction(self) -> Transaction:
        return await _SQLiteSessionTransaction(self).begin()

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
        staging = self._current.get()

        if staging is not None:
            if key in staging.delete_keys:
                return None
            staged = staging.writes.get(key)
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

        row = await self._client.read(read)
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

        rows = await self._client.read(read)
        by_key = {key: json.loads(call_stack) for key, call_stack in rows}

        staging = self._current.get()
        if staging is not None:
            for key in staging.delete_keys:
                by_key.pop(key, None)
            for key, row in staging.writes.items():
                if key.startswith(prefix):
                    by_key[key] = self._id_to_callstack(row.execution_id)

        return [self._callstack_to_id(call_stack) for call_stack in by_key.values()]

    async def delete_executions(self, execution_ids: Iterable[ExecutionId]) -> None:
        self._stage_delete_executions(execution_ids)
