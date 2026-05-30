from __future__ import annotations

import json
import linecache
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


class JsonLinesFileSessionStore(BaseFileSessionStore):
    """
    A file-based SessionStore that logs events to a JSON-Lines file, using an
    in-memory index for fast lookups without loading the entire log.
    This store is optimized for performance and scalability.
    """

    def __init__(self, client: FileClient, serializer: Serializer):
        super().__init__(client, serializer)
        self._executions_path = Path("executions.jsonl")
        self._index_path = Path("executions.jsonl.idx")

        self._index: dict[str, tuple[int, ExecutionStatus]] = {}
        self._results: dict[str, Any] = {}
        self._errors: dict[str, str] = {}
        self._staged_log_entries: list[LogEntry] = []
        self._is_loaded = False

    async def _ensure_loaded(self) -> None:
        if self._is_loaded:
            return
        async with self._lock:
            if self._is_loaded:
                return
            await self._load_index()
            self._is_loaded = True

    async def _load_index(self) -> None:
        """Loads the index from disk or rebuilds it if necessary."""
        log_content = await self._client.read(self._executions_path)
        if not log_content:
            return

        lines = log_content.decode("utf-8").splitlines()
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                entry: LogEntry = json.loads(line)
                key = self._callstack_to_key(entry["call_stack"])
                event_type = entry["event_type"]
                status = _EVENT_TYPE_TO_STATUS.get(event_type)

                if status:
                    self._index[key] = (i + 1, status)
                    if status == ExecutionStatus.COMPLETED:
                        self._results[key] = entry["result"]
                        self._errors.pop(key, None)
                    elif status == ExecutionStatus.FAILED:
                        self._errors[key] = entry["error"] or ""
                        self._results.pop(key, None)

            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(
                    "Skipping corrupted entry in %s at line %d: %s",
                    self._executions_path,
                    i + 1,
                    e,
                )
        linecache.clearcache()

    async def _add_log_entry(self, entry: LogEntry):
        async with self._lock:
            self._staged_log_entries.append(entry)

            async def writer() -> bytes:
                self._update_in_memory_state(entry)
                return (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")

            async def clear() -> None:
                try:
                    self._staged_log_entries.remove(entry)
                except ValueError:
                    pass

            await self._client.stage_append(self._executions_path, writer, clear)

    def _update_in_memory_state(self, entry: LogEntry) -> None:
        key = self._callstack_to_key(entry["call_stack"])
        status = _EVENT_TYPE_TO_STATUS.get(entry["event_type"])
        if not status:
            return

        # Simple line count approximation. A dedicated index file would make this robust.
        staged_count = len(self._client._staged_ops.get(str(self._executions_path), []))
        line_num = len(self._index) + staged_count
        self._index[key] = (line_num, status)
        if status == ExecutionStatus.COMPLETED:
            self._results[key] = entry["result"]
            self._errors.pop(key, None)
        elif status == ExecutionStatus.FAILED:
            self._errors[key] = entry["error"] or ""
            self._results.pop(key, None)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        await self._ensure_loaded()
        key = self._id_to_key(execution_id)

        if key not in self._index:
            return None

        _, status = self._index[key]
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
