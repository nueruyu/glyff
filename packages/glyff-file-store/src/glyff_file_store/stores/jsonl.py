from __future__ import annotations

import asyncio
import json
import linecache
import logging
from pathlib import Path
from typing import Any, TypedDict

from glyff.interfaces import Serializer
from glyff.models import ExecutionId, ExecutionRecord, ExecutionStatus

from ..file_client import FileClient
from ._base import (
    _EVENT_TYPE_TO_STATUS,
    BaseFileSessionStore,
    LogEntry,
)

logger = logging.getLogger(__name__)


class IndexEntry(TypedDict):
    k: str  # key
    n: int  # line number (1-based)
    s: str  # status ("start", "complete", "fail")


class JsonLinesFileSessionStore(BaseFileSessionStore):
    """
    A file-based SessionStore that logs events to a JSON-Lines file, using a
    persistent index file for fast lookups without loading the entire log.
    This store is optimized for performance and scalability.
    """

    def __init__(self, client: FileClient, serializer: Serializer):
        super().__init__(client, serializer)
        self._executions_path = Path("executions.jsonl")
        self._index_path = Path("executions.idx.jsonl")

        # Persistent in-memory view of committed state.
        self._index: dict[str, tuple[int, ExecutionStatus]] = {}
        self._results_cache: dict[str, Any] = {}
        self._errors: dict[str, str] = {}
        # 1-based number of the next line to be written in the log file.
        self._next_log_line: int = 1

        # Transactionally staged entries (not visible until commit).
        self._staged_log_entries: list[LogEntry] = []

        self._is_loaded = False

    async def _ensure_loaded(self) -> None:
        if self._is_loaded:
            return
        async with self._lock:
            if self._is_loaded:
                return
            await self._load_index_and_recover()
            self._is_loaded = True

    async def _load_index_and_recover(self) -> None:
        """Loads the index from disk and recovers from any previous crash."""
        index_content = await self._client.read(self._index_path)
        max_indexed_line = 0

        if index_content:
            lines = index_content.decode("utf-8").strip().splitlines()
            for line in lines:
                try:
                    idx_entry: IndexEntry = json.loads(line)
                    key = idx_entry["k"]
                    line_num = idx_entry["n"]
                    status_str = idx_entry["s"]
                    status = _EVENT_TYPE_TO_STATUS[status_str]
                    self._index[key] = (line_num, status)
                    if line_num > max_indexed_line:
                        max_indexed_line = line_num
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(
                        "Skipping corrupted entry in %s: %s",
                        self._index_path,
                        e,
                    )
                    continue

        log_content = await self._client.read(self._executions_path)
        if not log_content:
            self._next_log_line = 1
            linecache.clearcache()
            return

        log_lines = log_content.decode("utf-8").strip().splitlines()
        total_log_lines = len(log_lines)
        self._next_log_line = total_log_lines + 1

        if max_indexed_line > total_log_lines:
            # Index references lines that don't exist in the log (e.g. log was
            # truncated by a crashed commit). The log is canonical: rebuild the
            # in-memory state and the persisted index from it.
            self._index.clear()
            self._results_cache.clear()
            self._errors.clear()

            rebuilt_index_entries: list[IndexEntry] = []
            for i, raw_line in enumerate(log_lines):
                line_num = i + 1
                try:
                    log_entry: LogEntry = json.loads(raw_line)
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(
                        "Skipping corrupted log entry in %s at line %d: %s",
                        self._executions_path,
                        line_num,
                        e,
                    )
                    continue
                idx = self._apply_entry_to_state(log_entry, line_num)
                if idx is not None:
                    rebuilt_index_entries.append(idx)

            async def rebuilt_writer(
                entries: list[IndexEntry] = rebuilt_index_entries,
            ) -> bytes:
                return b"".join(
                    (json.dumps(e) + "\n").encode("utf-8") for e in entries
                )

            await self._client.stage_write(self._index_path, rebuilt_writer)
            await self._client.commit_staged()

        elif total_log_lines > max_indexed_line:
            # A crash may have occurred between log write and index update.
            # Process un-indexed log entries and append them to the index.
            recovered_index_entries: list[IndexEntry] = []
            for i in range(max_indexed_line, total_log_lines):
                line_num = i + 1
                try:
                    log_entry: LogEntry = json.loads(log_lines[i])
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(
                        "Skipping corrupted log entry in %s at line %d: %s",
                        self._executions_path,
                        line_num,
                        e,
                    )
                    continue
                idx = self._apply_entry_to_state(log_entry, line_num)
                if idx is not None:
                    recovered_index_entries.append(idx)

            if recovered_index_entries:

                async def append_writer(
                    entries: list[IndexEntry] = recovered_index_entries,
                ) -> bytes:
                    return b"".join(
                        (json.dumps(e) + "\n").encode("utf-8") for e in entries
                    )

                await self._client.stage_append(self._index_path, append_writer)
                await self._client.commit_staged()

        linecache.clearcache()

    def _apply_entry_to_state(
        self, entry: LogEntry, line_num: int
    ) -> IndexEntry | None:
        """Apply a log entry to the in-memory committed state. Returns the
        index entry that should be persisted to the index file, or None if the
        entry has no recognized status."""
        key = self._callstack_to_key(entry["call_stack"])
        status_str = entry["event_type"]
        status = _EVENT_TYPE_TO_STATUS.get(status_str)
        if not status:
            return None

        self._index[key] = (line_num, status)
        if status == ExecutionStatus.COMPLETED:
            self._results_cache[key] = entry["result"]
            self._errors.pop(key, None)
        elif status == ExecutionStatus.FAILED:
            self._errors[key] = entry["error"] or ""
            self._results_cache.pop(key, None)

        return IndexEntry(k=key, n=line_num, s=status_str)

    async def _on_log_clear(self) -> None:
        async with self._lock:
            self._staged_log_entries.clear()

    async def _add_log_entry(self, entry: LogEntry) -> None:
        async with self._lock:
            # Stage the writers for log and index files on the first entry of
            # the transaction. The writers are invoked during commit and update
            # the in-memory committed state at that point.
            if not self._staged_log_entries:
                # The shared list captured by both writers is populated by the
                # log writer (which knows the starting line number) and read by
                # the index writer.
                pending_index_entries: list[IndexEntry] = []

                async def log_writer() -> bytes:
                    chunks: list[bytes] = []
                    for staged in self._staged_log_entries:
                        line_num = self._next_log_line
                        self._next_log_line += 1
                        chunks.append(
                            (json.dumps(staged, sort_keys=True) + "\n").encode(
                                "utf-8"
                            )
                        )
                        idx = self._apply_entry_to_state(staged, line_num)
                        if idx is not None:
                            pending_index_entries.append(idx)
                    return b"".join(chunks)

                async def index_writer() -> bytes:
                    return b"".join(
                        (json.dumps(e) + "\n").encode("utf-8")
                        for e in pending_index_entries
                    )

                await self._client.stage_append(
                    self._executions_path, log_writer, self._on_log_clear
                )
                await self._client.stage_append(self._index_path, index_writer)

            self._staged_log_entries.append(entry)

    async def _read_log_line(self, line_num: int) -> LogEntry | None:
        abs_path = str(self._client.resolve(self._executions_path))
        line = await asyncio.to_thread(linecache.getline, abs_path, line_num)
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning(
                "Could not parse log line %d in %s: %s",
                line_num,
                self._executions_path,
                e,
            )
            return None

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        await self._ensure_loaded()
        key = self._id_to_key(execution_id)

        if key not in self._index:
            return None

        line_num, status = self._index[key]
        result: Any | None = None
        error: str | None = None

        if status == ExecutionStatus.COMPLETED:
            if key in self._results_cache:
                persistable_result = self._results_cache[key]
            else:
                log_entry = await self._read_log_line(line_num)
                persistable_result = log_entry.get("result") if log_entry else None
                if persistable_result is not None:
                    self._results_cache[key] = persistable_result

            if persistable_result is not None:
                serialized = json.dumps(persistable_result, sort_keys=True).encode(
                    "utf-8"
                )
                result = self._serializer.deserialize(serialized, return_type)

        elif status == ExecutionStatus.FAILED:
            if key in self._errors:
                error = self._errors[key]
            else:
                log_entry = await self._read_log_line(line_num)
                if log_entry is not None:
                    error = log_entry.get("error")
                    if error:
                        self._errors[key] = error

        return ExecutionRecord(status=status, result=result, error=error)
