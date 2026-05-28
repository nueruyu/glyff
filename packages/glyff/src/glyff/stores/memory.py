from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from ..interfaces import Execution, Serializer, SessionStore, Transaction
from ..models import ExecutionId, ExecutionRecord, ExecutionStatus
from .memory_client import MemoryClient


class _MemoryTransaction(Transaction):
    def __init__(self, client: MemoryClient):
        self._client = client

    async def commit(self) -> None:
        await self._client.commit_staged()

    async def rollback(self) -> None:
        self._client.clear_staged()


class _MemoryExecution(Execution):
    def __init__(self, store: MemorySessionStore, execution_id: ExecutionId):
        self._store = store
        self._id = execution_id

    def _id_to_key(self, id: ExecutionId, part: str) -> str:
        return f"execution::{id.name}#{id.sequence}:{id.args_hash}::{part}"

    async def complete(self, value: Any, return_type: type) -> None:
        self._store._client.stage_write(
            self._id_to_key(self._id, "status"), ExecutionStatus.COMPLETED
        )
        self._store._client.stage_write(
            self._id_to_key(self._id, "result"),
            self._store._serializer.serialize(value, return_type),
        )

    async def fail(self, error: str) -> None:
        self._store._client.stage_write(
            self._id_to_key(self._id, "status"), ExecutionStatus.FAILED
        )
        self._store._client.stage_write(self._id_to_key(self._id, "error"), error)

    async def yield_item(self, item: Any, item_type: Any) -> None:
        items_key = self._id_to_key(self._id, "items")
        # Seed from committed data so resume accumulates correctly
        existing_committed: list = self._store._client.data.get(items_key, [])
        existing_items: list = self._store._client._staged_writes.get(
            items_key, list(existing_committed)
        )
        serialized_item = self._store._serializer.serialize(item, item_type)
        existing_items.append(serialized_item)
        self._store._client.stage_write(items_key, existing_items)

    async def complete_stream(self) -> None:
        self._store._client.stage_write(
            self._id_to_key(self._id, "status"), ExecutionStatus.COMPLETED
        )


class MemorySessionStore(SessionStore):
    """
    An in-memory implementation of SessionStore for testing and development.
    This implementation is not persistent across processes.
    It serializes values to ensure independence, mimicking persisted stores.
    """

    def __init__(self, client: MemoryClient, serializer: Serializer, **_):
        self._client = client
        self._serializer = serializer
        self._lock = asyncio.Lock()

    def _id_to_key(self, id: ExecutionId, part: str) -> str:
        return f"execution::{id.name}#{id.sequence}:{id.args_hash}::{part}"

    async def begin_transaction(self) -> Transaction:
        return _MemoryTransaction(self._client)

    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        status_key = self._id_to_key(execution_id, "status")
        if await self._client.read(status_key) is None:
            self._client.stage_write(status_key, ExecutionStatus.STARTED)
        return _MemoryExecution(self, execution_id)

    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        status: ExecutionStatus | None = await self._client.read(
            self._id_to_key(execution_id, "status")
        )
        if not status:
            return None

        result = None
        error = None

        if status == ExecutionStatus.COMPLETED:
            serialized_value = await self._client.read(
                self._id_to_key(execution_id, "result")
            )
            if serialized_value:
                result = self._serializer.deserialize(serialized_value, return_type)
        elif status == ExecutionStatus.FAILED:
            error = await self._client.read(self._id_to_key(execution_id, "error"))

        return ExecutionRecord(status=status, result=result, error=error)

    async def get_stream_items(
        self, execution_id: ExecutionId, item_type: Any
    ) -> AsyncIterator[Any]:
        items_key = self._id_to_key(execution_id, "items")
        serialized_items: list | None = await self._client.read(items_key)
        if serialized_items:
            for s_item in serialized_items:
                yield self._serializer.deserialize(s_item, item_type)
