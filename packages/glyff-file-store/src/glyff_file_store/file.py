from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from glyff.interfaces import Execution, Serializer, SessionStore, Transaction
from glyff.models import ExecutionId, ExecutionRecord, ExecutionStatus

from .file_client import FileClient


class LogEntry(TypedDict):
    timestamp: str
    event_type: str  # "start", "complete", "fail"
    call_stack: list[str]  # outermost → innermost, each "name#sequence:args_hash"
    result: Any | None  # JSON-compatible object
    error: str | None


_STATUS_TO_EVENT_TYPE = {
    ExecutionStatus.STARTED: "start",
    ExecutionStatus.COMPLETED: "complete",
    ExecutionStatus.FAILED: "fail",
}
_EVENT_TYPE_TO_STATUS = {v: k for k, v in _STATUS_TO_EVENT_TYPE.items()}


class _FileTransaction(Transaction):
    def __init__(self, client: FileClient):
        self._client = client

    async def commit(self) -> None:
        await self._client.commit_staged()

    async def rollback(self) -> None:
        await self._client.clear_staged()


class _FileExecution(Execution):
    def __init__(self, store: FileSessionStore, execution_id: ExecutionId):
        self._store = store
        self._id = execution_id

    def _create_log_entry(self, status: ExecutionStatus, **kwargs) -> LogEntry:
        return LogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=_STATUS_TO_EVENT_TYPE[status],
            call_stack=self._store._id_to_callstack(self._id),
            result=kwargs.get("result"),
            error=kwargs.get("error"),
        )

    async def complete(self, value: Any, return_type: type) -> None:
        serialized_bytes = self._store._serializer.serialize(value, return_type)
        persistable_result = json.loads(serialized_bytes)
        entry = self._create_log_entry(
            ExecutionStatus.COMPLETED, result=persistable_result
        )
        await self._store._add_log_entry(entry)

    async def fail(self, error: str) -> None:
        entry = self._create_log_entry(ExecutionStatus.FAILED, error=error)
        await self._store._add_log_entry(entry)


class FileSessionStore(SessionStore):
    """
    A file-based implementation of SessionStore that logs all execution events
    to a file in either JSONL or pretty-printed JSON format.
    """

    def __init__(
        self,
        client: FileClient,
        serializer: Serializer,
        format: str = "jsonl",
        **kwargs,
    ):
        if format not in ("json", "jsonl"):
            raise ValueError("format must be either 'json' or 'jsonl'")
        self._client = client
        self._serializer = serializer
        self._format = format
        self._executions_path = Path(f"executions.{format}")

        self._states: dict[str, ExecutionStatus] = {}
        self._results: dict[str, Any] = {}  # Caches JSON-compatible objects
        self._errors: dict[str, str] = {}

        self._staged_log_entries: list[LogEntry] = []
        self._lock = asyncio.Lock()

        self._load_executions()

    @property
    def executions_path(self) -> Path:
        return self._executions_path

    def _id_to_callstack(self, execution_id: ExecutionId) -> list[str]:
        """Converts an ExecutionId to a call stack list (outermost → innermost)."""
        frames: list[str] = []
        current: ExecutionId | None = execution_id
        while current is not None:
            frames.append(f"{current.name}#{current.sequence}:{current.args_hash}")
            current = current.parent_id
        frames.reverse()
        return frames

    def _id_to_key(self, execution_id: ExecutionId) -> str:
        """Converts an ExecutionId to a stable, unique string key."""
        parent_path = (
            f"{self._id_to_key(execution_id.parent_id)}/"
            if execution_id.parent_id
            else ""
        )
        return f"{parent_path}{execution_id.name}#{execution_id.sequence}:{execution_id.args_hash}"

    @staticmethod
    def _callstack_to_key(call_stack: list[str]) -> str:
        """Converts a call stack list back to the stable string key."""
        return "/".join(call_stack)

    def _load_executions(self) -> None:
        """Parses the executions file to build the in-memory state."""
        abs_path = self._client.resolve(self._executions_path)
        if not abs_path.exists():
            return

        lines_to_process: list[str] = []
        with open(abs_path, "r", encoding="utf-8") as f:
            if self._format == "jsonl":
                lines_to_process.extend(f.readlines())
            else:  # json
                content = f.read()
                if not content.strip():
                    return
                try:
                    entries = json.loads(content)
                    lines_to_process.extend([json.dumps(e) for e in entries])
                except json.JSONDecodeError:
                    return  # Ignore corrupted file

        for line in lines_to_process:
            if not line.strip():
                continue
            try:
                entry: LogEntry = json.loads(line)
                key = self._callstack_to_key(entry["call_stack"])
                event_type = entry["event_type"]

                status = _EVENT_TYPE_TO_STATUS.get(event_type)

                if status is ExecutionStatus.STARTED:
                    self._states[key] = ExecutionStatus.STARTED
                elif status is ExecutionStatus.COMPLETED:
                    self._states[key] = ExecutionStatus.COMPLETED
                    self._results[key] = entry["result"]
                    if key in self._errors:
                        del self._errors[key]
                elif status is ExecutionStatus.FAILED:
                    self._states[key] = ExecutionStatus.FAILED
                    self._errors[key] = entry["error"] or ""
                    if key in self._results:
                        del self._results[key]
            except (json.JSONDecodeError, KeyError):
                pass

    async def _on_write(self) -> bytes:
        staged_entries = self._staged_log_entries
        existing_content = await self._client.read(self._executions_path) or b""

        if self._format == "jsonl":
            content_to_append = ""
            for entry in staged_entries:
                content_to_append += json.dumps(entry, sort_keys=True) + "\n"

            self._update_in_memory_state(staged_entries)
            return existing_content + content_to_append.encode("utf-8")
        else:  # json
            all_entries: list[LogEntry] = []
            if existing_content:
                try:
                    all_entries = json.loads(existing_content.decode("utf-8"))
                except json.JSONDecodeError:
                    pass
            all_entries.extend(staged_entries)
            new_content = json.dumps(all_entries, indent=2, sort_keys=True)

            self._update_in_memory_state(staged_entries)
            return new_content.encode("utf-8")

    async def _on_clear(self) -> None:
        self._staged_log_entries.clear()

    def _update_in_memory_state(self, entries: list[LogEntry]):
        for entry in entries:
            key = self._callstack_to_key(entry["call_stack"])
            status = _EVENT_TYPE_TO_STATUS.get(entry["event_type"])
            if status is ExecutionStatus.STARTED:
                self._states[key] = ExecutionStatus.STARTED
            elif status is ExecutionStatus.COMPLETED:
                self._states[key] = ExecutionStatus.COMPLETED
                self._results[key] = entry["result"]
            elif status is ExecutionStatus.FAILED:
                self._states[key] = ExecutionStatus.FAILED
                self._errors[key] = entry["error"] or ""

    async def _add_log_entry(self, entry: LogEntry):
        async with self._lock:
            # If this is the first entry in the transaction, stage the write operation.
            if not self._staged_log_entries:
                await self._client.stage_write(
                    self._executions_path,
                    self._on_write,
                    self._on_clear,
                )

            self._staged_log_entries.append(entry)

    async def begin_transaction(self) -> Transaction:
        self._staged_log_entries.clear()
        return _FileTransaction(self._client)

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        key = self._id_to_key(execution_id)
        if self._states.get(key) is None:
            entry = LogEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type=_STATUS_TO_EVENT_TYPE[ExecutionStatus.STARTED],
                call_stack=self._id_to_callstack(execution_id),
                result=None,
                error=None,
            )
            await self._add_log_entry(entry)
        return _FileExecution(self, execution_id)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        key = self._id_to_key(execution_id)
        status = self._states.get(key)
        if not status:
            return None

        result = None
        error = None

        if status == ExecutionStatus.COMPLETED:
            persistable_result = self._results.get(key)
            if persistable_result is not None:
                serialized_bytes = json.dumps(
                    persistable_result, sort_keys=True
                ).encode("utf-8")
                result = self._serializer.deserialize(serialized_bytes, return_type)
        elif status == ExecutionStatus.FAILED:
            error = self._errors.get(key)

        return ExecutionRecord(status=status, result=result, error=error)
