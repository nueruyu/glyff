from __future__ import annotations

import asyncio
import contextvars
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _SQLiteEvent:
    execution_id: ExecutionId
    status: ExecutionStatus
    result: bytes | None = None
    error: str | None = None


class _SQLiteStaging:
    """A single transaction's pending events and deletes."""

    __slots__ = ("events", "delete_keys")

    def __init__(self) -> None:
        self.events: list[_SQLiteEvent] = []
        self.delete_keys: set[str] = set()


class _SQLiteTransaction(Transaction):
    def __init__(self, store: SQLiteSessionStore):
        self._store = store
        self._closed = False
        # Isolate this transaction's staging from any concurrent transaction.
        self._token = store.begin_staging()

    async def commit(self) -> None:
        if self._closed:
            return
        try:
            await self._store._commit_staged()
        finally:
            self._store.end_staging(self._token)
            self._closed = True

    async def rollback(self) -> None:
        if self._closed:
            return
        try:
            await self._store._clear_staged()
        finally:
            self._store.end_staging(self._token)
            self._closed = True


class _SQLiteExecution(Execution):
    def __init__(
        self,
        execution_id: ExecutionId,
        serializer: Serializer,
        append_event: Callable[[_SQLiteEvent], Awaitable[None]],
    ):
        self._execution_id = execution_id
        self._serializer = serializer
        self._append_event = append_event

    async def complete(self, value: object, return_type: type) -> None:
        serialized_bytes = await self._serializer.serialize(value, return_type)
        await self._append_event(
            _SQLiteEvent(
                execution_id=self._execution_id,
                status=ExecutionStatus.COMPLETED,
                result=serialized_bytes,
            )
        )

    async def fail(self, error: str) -> None:
        await self._append_event(
            _SQLiteEvent(
                execution_id=self._execution_id,
                status=ExecutionStatus.FAILED,
                error=error,
            )
        )


class SQLiteSessionStore(SessionStore):
    """
    A SQLite-backed SessionStore for durable local persistence.

    The store keeps one row per execution id and uses WAL mode for atomic,
    indexed updates. It is the durable backend; JsonFileSessionStore remains the
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
        self._lock = asyncio.Lock()
        self._ambient = _SQLiteStaging()
        # Per-instance so concurrent transactions (e.g. parallel gather
        # branches, each in a copied context) stage in isolation.
        self._current: contextvars.ContextVar[_SQLiteStaging | None] = (
            contextvars.ContextVar("sqlite_staging", default=None)
        )
        self._initialize_database()

    def _staging(self) -> _SQLiteStaging:
        """The staging buffer for the current transaction, or the ambient one."""
        return self._current.get() or self._ambient

    def begin_staging(self) -> contextvars.Token:
        return self._current.set(_SQLiteStaging())

    def end_staging(self, token: contextvars.Token) -> None:
        try:
            self._current.reset(token)
        except (ValueError, LookupError):
            pass

    # ------------------------------------------------------------------
    # SQLite helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            isolation_level=None,
            timeout=self._busy_timeout_ms / 1000,
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

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _callstack_to_key(call_stack: list[str]) -> str:
        return "/".join(call_stack)

    @staticmethod
    def _callstack_to_id(call_stack: list[str]) -> ExecutionId:
        return path_to_execution_id("/".join(call_stack))

    def _id_to_callstack(self, execution_id: ExecutionId) -> list[str]:
        return execution_id_to_path(execution_id).split("/")

    def _id_to_key(self, execution_id: ExecutionId) -> str:
        return execution_id_to_path(execution_id)

    # ------------------------------------------------------------------
    # Staging and commit
    # ------------------------------------------------------------------

    async def _append_event(self, event: _SQLiteEvent) -> None:
        async with self._lock:
            self._staging().events.append(event)

    async def _clear_staged(self) -> None:
        async with self._lock:
            staging = self._staging()
            staging.events.clear()
            staging.delete_keys.clear()

    async def _commit_staged(self) -> None:
        async with self._lock:
            staging = self._staging()
            events = list(staging.events)
            delete_keys = set(staging.delete_keys)
            if not events and not delete_keys:
                return

            await asyncio.to_thread(self._write_committed, events, delete_keys)
            staging.events.clear()
            staging.delete_keys.clear()

    def _write_committed(
        self, events: list[_SQLiteEvent], delete_keys: set[str]
    ) -> None:
        connection = self._connect()
        in_transaction = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            in_transaction = True
            for key in delete_keys:
                connection.execute("DELETE FROM executions WHERE key = ?", (key,))

            for event in events:
                key = self._id_to_key(event.execution_id)
                if key in delete_keys:
                    continue

                call_stack = self._id_to_callstack(event.execution_id)
                # The serializer already produced JSON bytes; decode them into
                # the TEXT column without a parse/re-serialize round-trip.
                result_json = (
                    None
                    if event.result is None
                    else event.result.decode(DEFAULT_ENCODING)
                )
                connection.execute(
                    """
                    INSERT INTO executions (
                        key, call_stack_json, status, result_json, error, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        call_stack_json = excluded.call_stack_json,
                        status = excluded.status,
                        result_json = excluded.result_json,
                        error = excluded.error,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        json.dumps(call_stack, separators=JSON_SEPARATORS),
                        _STATUS_TO_EVENT_TYPE[event.status],
                        result_json,
                        event.error,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            connection.execute("COMMIT")
            in_transaction = False
        except Exception:
            if in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    async def begin_transaction(self) -> Transaction:
        return _SQLiteTransaction(self)

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        record = await self.get_execution_record(execution_id, type(None))
        if record is None:
            await self._append_event(
                _SQLiteEvent(
                    execution_id=execution_id,
                    status=ExecutionStatus.STARTED,
                )
            )
        return _SQLiteExecution(
            execution_id=execution_id,
            serializer=self._serializer,
            append_event=self._append_event,
        )

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        key = self._id_to_key(execution_id)
        row = await asyncio.to_thread(self._read_record_row, key)
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

    def _read_record_row(self, key: str) -> tuple[str, str | None, str | None] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT status, result_json, error FROM executions WHERE key = ?",
                (key,),
            ).fetchone()
            return row
        finally:
            connection.close()

    async def get_descendants(self, execution_id: ExecutionId) -> list[ExecutionId]:
        prefix = self._id_to_key(execution_id) + "/"
        committed_rows = await asyncio.to_thread(self._read_descendant_rows, prefix)

        keys: dict[str, list[str]] = {
            key: json.loads(call_stack_json) for key, call_stack_json in committed_rows
        }
        async with self._lock:
            staging = self._staging()
            for key in staging.delete_keys:
                keys.pop(key, None)
            for event in staging.events:
                key = self._id_to_key(event.execution_id)
                if key.startswith(prefix) and key not in staging.delete_keys:
                    keys[key] = self._id_to_callstack(event.execution_id)

        return [self._callstack_to_id(call_stack) for call_stack in keys.values()]

    def _read_descendant_rows(self, prefix: str) -> list[tuple[str, str]]:
        pattern = self._escape_like(prefix) + "%"
        connection = self._connect()
        try:
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
        finally:
            connection.close()

    async def delete_executions(self, execution_ids: Iterable[ExecutionId]) -> None:
        keys = [self._id_to_key(eid) for eid in execution_ids]
        if not keys:
            return
        async with self._lock:
            self._staging().delete_keys.update(keys)
