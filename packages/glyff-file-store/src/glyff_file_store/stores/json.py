from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from glyff.interfaces import Serializer
from glyff.models import ExecutionId, ExecutionRecord, ExecutionStatus

from ..file_client import FileClient
from ._base import (
    _EVENT_TYPE_TO_STATUS,
    BaseFileSessionStore,
    LogEntry,
)

logger = logging.getLogger(__name__)


class JsonFileSessionStore(BaseFileSessionStore):
    """
    A file-based SessionStore that logs events to a pretty-printed JSON file.
    Note: This store reads the entire session state into memory on load and is
    intended primarily for debugging small-scale sessions. For performance and
    scalability, use JsonLinesFileSessionStore.
    """

    def __init__(self, client: FileClient, serializer: Serializer):
        super().__init__(client, serializer)
        self._executions_path = Path("executions.json")
        self._states: dict[str, ExecutionStatus] = {}
        self._results: dict[str, Any] = {}
        self._errors: dict[str, str] = {}
        self._staged_log_entries: list[LogEntry] = []
        self._load_executions()

    def _load_executions(self) -> None:
        abs_path = self._client.resolve(self._executions_path)
        if not abs_path.exists():
            return

        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
            if not content.strip():
                return
            try:
                entries = json.loads(content)
                self._update_in_memory_state(entries)
            except json.JSONDecodeError as e:
                logger.warning(
                    "Could not parse executions log %s; ignoring corrupted file: %s",
                    abs_path,
                    e,
                )
                return

    async def _on_write(self) -> bytes:
        all_entries: list[LogEntry] = []
        existing_content = await self._client.read(self._executions_path) or b""
        if existing_content:
            try:
                all_entries = json.loads(existing_content.decode("utf-8"))
            except json.JSONDecodeError as e:
                logger.warning(
                    "Could not parse existing executions log at %s on write; "
                    "discarding prior entries: %s",
                    self._executions_path,
                    e,
                )
        all_entries.extend(self._staged_log_entries)
        new_content = json.dumps(all_entries, indent=2, sort_keys=True)
        self._update_in_memory_state(self._staged_log_entries)
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
            if not self._staged_log_entries:
                await self._client.stage_write(
                    self._executions_path,
                    self._on_write,
                    self._on_clear,
                )
            self._staged_log_entries.append(entry)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        key = self._id_to_key(execution_id)
        status = self._states.get(key)
        if not status:
            return None

        result: Any | None = None
        error: str | None = None
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
