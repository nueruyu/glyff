from __future__ import annotations

import asyncio
import json
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
    k: str  # call-stack key
    o: int  # byte offset of the entry in the log file
    s: str  # status ("start", "complete", "fail")


def _serialize_log_entry(entry: LogEntry) -> bytes:
    return (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")


def _serialize_index_entry(entry: IndexEntry) -> bytes:
    return (json.dumps(entry) + "\n").encode("utf-8")


class JsonLinesFileSessionStore(BaseFileSessionStore):
    """
    A file-based SessionStore that logs events to a JSON-Lines file, using a
    persistent index file for fast lookups without loading the entire log.
    Each index entry stores a byte offset into the log file so that lazy
    loads of result/error payloads are O(1) on disk.
    """

    def __init__(self, client: FileClient, serializer: Serializer):
        super().__init__(client, serializer)
        self._executions_path = Path("executions.jsonl")
        self._index_path = Path("executions.idx.jsonl")

        # Committed state.
        self._index: dict[str, tuple[int, ExecutionStatus]] = {}
        self._results_cache: dict[str, Any] = {}
        self._errors: dict[str, str] = {}
        # Byte size of the on-disk log file (= offset where the next append
        # will land).
        self._log_size: int = 0

        # Transactionally staged entries (not visible until commit).
        self._staged_log_entries: list[LogEntry] = []

        self._is_loaded = False

    # ------------------------------------------------------------------
    # Loading and crash recovery
    # ------------------------------------------------------------------

    async def _ensure_loaded(self) -> None:
        if self._is_loaded:
            return
        async with self._lock:
            if self._is_loaded:
                return
            await self._load_index_and_recover()
            self._is_loaded = True

    async def _load_index_from_file(self) -> int:
        """Loads the index file into memory and returns the max offset found.
        Returns -1 if the index file is empty or missing."""
        max_indexed_offset = -1
        index_content = await self._client.read(self._index_path)
        if not index_content:
            return max_indexed_offset

        for line in index_content.decode("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                idx_entry: IndexEntry = json.loads(line)
                key = idx_entry["k"]
                offset = idx_entry["o"]
                status_str = idx_entry["s"]
                status = _EVENT_TYPE_TO_STATUS[status_str]
                self._index[key] = (offset, status)
                if offset > max_indexed_offset:
                    max_indexed_offset = offset
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(
                    "Skipping corrupted entry in %s: %s",
                    self._index_path,
                    e,
                )
        return max_indexed_offset

    async def _rebuild_index_from_log(
        self, log_offsets: list[int], log_entries: list[bytes]
    ) -> None:
        """Rebuild the entire index from the log file, overwriting the index
        file on disk. Used when the index references offsets past the end of
        the log (e.g. a crashed commit truncated the log)."""
        logger.warning(
            "Rebuilding index for %s from canonical log", self._executions_path
        )
        self._index.clear()
        self._results_cache.clear()
        self._errors.clear()
        rebuilt: list[IndexEntry] = []
        for offset, raw in zip(log_offsets, log_entries):
            idx = self._parse_and_apply(raw, offset)
            if idx is not None:
                rebuilt.append(idx)
        await self._write_index_file(rebuilt, append=False)

    async def _catch_up_index_from_log(
        self,
        log_offsets: list[int],
        log_entries: list[bytes],
        max_indexed_offset: int,
    ) -> None:
        """Append any un-indexed entries from the log to the index file. Used
        when a previous commit wrote the log but crashed before updating the
        index."""
        recovered: list[IndexEntry] = []
        for offset, raw in zip(log_offsets, log_entries):
            if offset <= max_indexed_offset:
                continue
            idx = self._parse_and_apply(raw, offset)
            if idx is not None:
                recovered.append(idx)
        if recovered:
            logger.info(
                "Recovered %d un-indexed entries for %s",
                len(recovered),
                self._executions_path,
            )
            await self._write_index_file(recovered, append=True)

    async def _load_index_and_recover(self) -> None:
        """Loads the index from disk and recovers from any previous crash.

        Recovery uses direct disk writes (via ``asyncio.to_thread``) rather
        than the FileClient staging queue, so the user's outer transaction
        state is never observed or modified.
        """
        max_indexed_offset = await self._load_index_from_file()

        log_content = await self._client.read(self._executions_path)
        if not log_content:
            self._log_size = 0
            return

        self._log_size = len(log_content)
        log_offsets, log_entries = _scan_log_offsets(log_content)
        max_valid_offset = log_offsets[-1] if log_offsets else -1

        if max_indexed_offset > max_valid_offset:
            await self._rebuild_index_from_log(log_offsets, log_entries)
        else:
            await self._catch_up_index_from_log(
                log_offsets, log_entries, max_indexed_offset
            )

    def _parse_and_apply(
        self, raw_line: bytes, offset: int
    ) -> IndexEntry | None:
        try:
            entry: LogEntry = json.loads(raw_line)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(
                "Skipping corrupted log entry in %s at offset %d: %s",
                self._executions_path,
                offset,
                e,
            )
            return None
        return self._apply_entry_to_state(entry, offset)

    async def _write_index_file(
        self, entries: list[IndexEntry], append: bool
    ) -> None:
        """Write directly to the index file, bypassing the staging queue."""
        if not entries and not append:
            # Rebuild with nothing to write: clear the file.
            payload = b""
        else:
            payload = b"".join(_serialize_index_entry(e) for e in entries)

        abs_path = self._client.resolve(self._index_path)

        def _write() -> None:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            mode = "ab" if append else "wb"
            with open(abs_path, mode) as f:
                f.write(payload)

        await asyncio.to_thread(_write)

    def _apply_entry_to_state(
        self, entry: LogEntry, offset: int
    ) -> IndexEntry | None:
        """Apply a log entry to the in-memory committed state. Returns the
        matching index entry, or None if the entry has no recognized status."""
        key = self._callstack_to_key(entry["call_stack"])
        status_str = entry["event_type"]
        status = _EVENT_TYPE_TO_STATUS.get(status_str)
        if not status:
            return None

        self._index[key] = (offset, status)
        if status == ExecutionStatus.COMPLETED:
            self._results_cache[key] = entry["result"]
            self._errors.pop(key, None)
        elif status == ExecutionStatus.FAILED:
            self._errors[key] = entry["error"] or ""
            self._results_cache.pop(key, None)
        return IndexEntry(k=key, o=offset, s=status_str)

    # ------------------------------------------------------------------
    # Staging and commit
    # ------------------------------------------------------------------

    async def _add_log_entry(self, entry: LogEntry) -> None:
        async with self._lock:
            if not self._staged_log_entries:
                # Stage writers on the first entry of the transaction. Each
                # writer reads `_staged_log_entries` and `_log_size` at commit
                # time and produces bytes; neither mutates store state, so
                # commit failures cannot leave the store in a partial state.
                async def log_writer() -> bytes:
                    return b"".join(
                        _serialize_log_entry(e)
                        for e in self._staged_log_entries
                    )

                async def index_writer() -> bytes:
                    offset = self._log_size
                    chunks: list[bytes] = []
                    for e in self._staged_log_entries:
                        line = _serialize_log_entry(e)
                        status_str = e["event_type"]
                        if status_str in _EVENT_TYPE_TO_STATUS:
                            key = self._callstack_to_key(e["call_stack"])
                            chunks.append(
                                _serialize_index_entry(
                                    IndexEntry(k=key, o=offset, s=status_str)
                                )
                            )
                        offset += len(line)
                    return b"".join(chunks)

                await self._client.stage_append(
                    self._executions_path, log_writer
                )
                await self._client.stage_append(
                    self._index_path, index_writer
                )

            self._staged_log_entries.append(entry)

    async def _on_transaction_commit(self) -> None:
        async with self._lock:
            offset = self._log_size
            for entry in self._staged_log_entries:
                line = _serialize_log_entry(entry)
                self._apply_entry_to_state(entry, offset)
                offset += len(line)
            self._log_size = offset
            self._staged_log_entries.clear()

    async def _on_transaction_rollback(self) -> None:
        async with self._lock:
            self._staged_log_entries.clear()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def _read_log_line_at(self, offset: int) -> LogEntry | None:
        abs_path = self._client.resolve(self._executions_path)

        def _read() -> bytes:
            with open(abs_path, "rb") as f:
                f.seek(offset)
                return f.readline()

        try:
            line = await asyncio.to_thread(_read)
        except FileNotFoundError:
            return None
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning(
                "Could not parse log entry at offset %d in %s: %s",
                offset,
                self._executions_path,
                e,
            )
            return None

    async def _get_payload_from_log(
        self,
        key: str,
        offset: int,
        cache: dict[str, Any],
        payload_field: str,
    ) -> Any:
        """Return the payload at ``payload_field`` of the log entry at
        ``offset``, populating ``cache`` on first read. Returns None if the
        entry is missing or has no payload."""
        if key in cache:
            return cache[key]
        log_entry = await self._read_log_line_at(offset)
        if log_entry is None:
            return None
        payload = log_entry.get(payload_field)
        if payload is not None:
            cache[key] = payload
        return payload

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        await self._ensure_loaded()
        key = self._id_to_key(execution_id)

        if key not in self._index:
            return None

        offset, status = self._index[key]
        result: Any | None = None
        error: str | None = None

        if status == ExecutionStatus.COMPLETED:
            persistable_result = await self._get_payload_from_log(
                key, offset, self._results_cache, "result"
            )
            if persistable_result is not None:
                serialized = json.dumps(persistable_result, sort_keys=True).encode(
                    "utf-8"
                )
                result = self._serializer.deserialize(serialized, return_type)

        elif status == ExecutionStatus.FAILED:
            error = await self._get_payload_from_log(
                key, offset, self._errors, "error"
            )

        return ExecutionRecord(status=status, result=result, error=error)


def _scan_log_offsets(log_content: bytes) -> tuple[list[int], list[bytes]]:
    """Return parallel lists of (byte offset, raw line bytes) for each
    non-empty line in ``log_content``. Offsets are absolute positions in the
    file."""
    offsets: list[int] = []
    lines: list[bytes] = []
    pos = 0
    total = len(log_content)
    while pos < total:
        newline = log_content.find(b"\n", pos)
        end = newline if newline != -1 else total
        if end > pos:
            offsets.append(pos)
            lines.append(log_content[pos:end])
        pos = end + 1
    return offsets, lines
