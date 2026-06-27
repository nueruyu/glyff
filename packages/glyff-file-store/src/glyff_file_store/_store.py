from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

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

from ._file_client import FileClient

logger = logging.getLogger(__name__)


class LogEntry(TypedDict):
    timestamp: str
    event_type: str  # "start", "complete", "fail"
    call_stack: list[str]
    result: object | None
    error: str | None


_STATUS_TO_EVENT_TYPE = {
    ExecutionStatus.STARTED: "start",
    ExecutionStatus.COMPLETED: "complete",
    ExecutionStatus.FAILED: "fail",
}
_EVENT_TYPE_TO_STATUS = {v: k for k, v in _STATUS_TO_EVENT_TYPE.items()}


def _serialized_result_to_log_value(serialized: bytes) -> object:
    return json.loads(serialized.decode(DEFAULT_ENCODING))


def _log_value_to_serialized_result(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=JSON_SEPARATORS).encode(
        DEFAULT_ENCODING
    )


class _StagingBuffer:
    __slots__ = ("entries", "delete_keys")

    def __init__(self) -> None:
        self.entries: list[LogEntry] = []
        self.delete_keys: set[str] = set()

    def clear(self) -> None:
        self.entries.clear()
        self.delete_keys.clear()


class _FileTransaction(Transaction):
    def __init__(self, store: JsonFileSessionStore):
        self._store = store
        self._closed = False
        self._lock = asyncio.Lock()
        self._store_token, self._store_staging = store.begin_staging()
        self._client_token, self._client_staging = store._client.begin_staging()

    async def commit(self) -> None:
        async with self._lock:
            if self._closed:
                return

            self._store._require_current_staging(self._store_staging)
            self._store._client._require_current_staging(self._client_staging)

            self._closed = True
            try:
                await self._store._commit_current()
            finally:
                self._store._client.end_staging(self._client_token)
                self._store.end_staging(self._store_token)

    async def rollback(self) -> None:
        async with self._lock:
            if self._closed:
                return

            self._store._require_current_staging(self._store_staging)
            self._store._client._require_current_staging(self._client_staging)

            self._closed = True
            try:
                await self._store._rollback_current()
                await self._store._client.clear_staged()
            finally:
                self._store._client.end_staging(self._client_token)
                self._store.end_staging(self._store_token)


class _FileExecution(Execution):
    def __init__(
        self,
        call_stack: list[str],
        serializer: JsonSerializer,
        append_entry: Callable[[LogEntry], Awaitable[None]],
    ):
        self._call_stack = call_stack
        self._serializer = serializer
        self._append_entry = append_entry

    def _create_log_entry(
        self,
        status: ExecutionStatus,
        result: object | None = None,
        error: str | None = None,
    ) -> LogEntry:
        return LogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=_STATUS_TO_EVENT_TYPE[status],
            call_stack=self._call_stack,
            result=result,
            error=error,
        )

    async def complete(self, value: object, return_type: type) -> None:
        serialized_bytes = await self._serializer.serialize(value, return_type)
        persistable_result = _serialized_result_to_log_value(serialized_bytes)
        entry = self._create_log_entry(
            ExecutionStatus.COMPLETED, result=persistable_result
        )
        await self._append_entry(entry)

    async def fail(self, error: str) -> None:
        entry = self._create_log_entry(ExecutionStatus.FAILED, error=error)
        await self._append_entry(entry)


class JsonFileSessionStore(SessionStore):
    """Human-readable debug SessionStore backed by a pretty-printed JSON log.

    The whole log is kept in memory and rewritten atomically on each commit
    (O(n) per commit), so it suits small sessions.
    """

    def __init__(self, client: FileClient, serializer: JsonSerializer):
        self._client = client
        self._serializer = serializer
        self._lock = asyncio.Lock()
        self._executions_path = Path("executions.json")
        self._log_entries: list[LogEntry] = []
        self._latest_index: dict[str, int] = {}
        self._current: contextvars.ContextVar[_StagingBuffer | None] = (
            contextvars.ContextVar("json_store_staging", default=None)
        )
        self._load_executions()

    def _id_to_callstack(self, execution_id: ExecutionId) -> list[str]:
        return execution_id_to_path(execution_id).split("/")

    def _id_to_key(self, execution_id: ExecutionId) -> str:
        return execution_id_to_path(execution_id)

    @staticmethod
    def _callstack_to_key(call_stack: list[str]) -> str:
        return "/".join(call_stack)

    @staticmethod
    def _callstack_to_id(call_stack: list[str]) -> ExecutionId:
        return path_to_execution_id("/".join(call_stack))

    def _load_executions(self) -> None:
        abs_path = self._client.resolve(self._executions_path)
        if not abs_path.exists():
            return

        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return
        try:
            entries: list[LogEntry] = json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning(
                "Could not parse executions log %s; ignoring corrupted file: %s",
                abs_path,
                e,
            )
            return

        self._log_entries = entries
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._latest_index.clear()
        for i, entry in enumerate(self._log_entries):
            if entry["event_type"] in _EVENT_TYPE_TO_STATUS:
                key = self._callstack_to_key(entry["call_stack"])
                self._latest_index[key] = i

    def _require_staging(self) -> _StagingBuffer:
        staging = self._current.get()
        if staging is None:
            raise RuntimeError(
                "JsonFileSessionStore write attempted outside a transaction."
            )
        return staging

    def _require_current_staging(self, expected: _StagingBuffer) -> None:
        if self._current.get() is not expected:
            raise RuntimeError("Transaction closed out of order.")

    def begin_staging(self) -> tuple[contextvars.Token, _StagingBuffer]:
        staging = _StagingBuffer()
        token = self._current.set(staging)
        return token, staging

    def end_staging(self, token: contextvars.Token) -> None:
        try:
            self._current.reset(token)
        except (ValueError, LookupError):
            pass

    async def _add_log_entry(self, entry: LogEntry) -> None:
        staging = self._require_staging()
        staging.delete_keys.discard(self._callstack_to_key(entry["call_stack"]))
        staging.entries.append(entry)

    async def _commit_current(self) -> None:
        staging = self._require_staging()
        async with self._lock:
            if not staging.entries and not staging.delete_keys:
                return

            merged = self._log_entries + staging.entries
            if staging.delete_keys:
                merged = [
                    e
                    for e in merged
                    if self._callstack_to_key(e["call_stack"])
                    not in staging.delete_keys
                ]
            await self._write_all(self._serialize_entries(merged))
            self._log_entries = merged
            self._rebuild_index()
            staging.clear()

    async def _rollback_current(self) -> None:
        staging = self._require_staging()
        staging.clear()

    @staticmethod
    def _serialize_entries(entries: list[LogEntry]) -> bytes:
        return json.dumps(
            entries, indent=2, sort_keys=True, separators=JSON_SEPARATORS
        ).encode(DEFAULT_ENCODING)

    async def _write_all(self, content: bytes) -> None:
        await self._client.stage_write(self._executions_path, content)
        await self._client.commit_staged()

    async def begin_transaction(self) -> Transaction:
        return _FileTransaction(self)

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        record = await self.get_execution_record(execution_id, type(None))
        if record is None:
            entry = LogEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type=_STATUS_TO_EVENT_TYPE[ExecutionStatus.STARTED],
                call_stack=self._id_to_callstack(execution_id),
                result=None,
                error=None,
            )
            await self._add_log_entry(entry)
        return _FileExecution(
            call_stack=self._id_to_callstack(execution_id),
            serializer=self._serializer,
            append_entry=self._add_log_entry,
        )

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        key = self._id_to_key(execution_id)
        idx = self._latest_index.get(key)
        if idx is None:
            return None

        entry = self._log_entries[idx]
        return await self._entry_to_record(entry, return_type)

    async def _entry_to_record(
        self, entry: LogEntry, return_type: type
    ) -> ExecutionRecord:
        status = _EVENT_TYPE_TO_STATUS[entry["event_type"]]
        result: Any | None = None
        error: str | None = None
        if status == ExecutionStatus.COMPLETED:
            persistable_result = entry["result"]
            if persistable_result is not None:
                result = await self._serializer.deserialize(
                    _log_value_to_serialized_result(persistable_result), return_type
                )
        elif status == ExecutionStatus.FAILED:
            error = entry["error"] or ""
        return ExecutionRecord(status=status, result=result, error=error)

    async def get_descendants(self, execution_id: ExecutionId) -> list[ExecutionId]:
        prefix = self._id_to_key(execution_id) + "/"
        keys: dict[str, list[str]] = {}
        for entry in self._log_entries:
            key = self._callstack_to_key(entry["call_stack"])
            if key.startswith(prefix):
                keys.setdefault(key, entry["call_stack"])
        return [self._callstack_to_id(call_stack) for call_stack in keys.values()]

    async def delete_executions(self, execution_ids: Iterable[ExecutionId]) -> None:
        keys = {execution_id_to_path(eid) for eid in execution_ids}
        if not keys:
            return
        staging = self._require_staging()
        staging.delete_keys.update(keys)
