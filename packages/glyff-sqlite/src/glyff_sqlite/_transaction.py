from __future__ import annotations

import asyncio

from glyff import Transaction
from glyff.store.staging import ExecutionStage, ExecutionStaging

from glyff_sqlite._sqlite_client import SQLiteClient


class ClientTransaction(Transaction):
    def __init__(self, client: SQLiteClient, staging: ExecutionStaging) -> None:
        self._client = client
        self._staging = staging
        self._stage: ExecutionStage | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def begin(self) -> ClientTransaction:
        self._stage = self._staging.begin()
        return self

    async def commit(self) -> None:
        async with self._lock:
            if self._closed:
                return
            stage = self._require_stage()
            stage.close()
            self._closed = True
            await self._client.commit_mutations(stage.batch)

    async def rollback(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._require_stage().close()
            self._closed = True

    def _require_stage(self) -> ExecutionStage:
        if self._stage is None:
            raise RuntimeError("transaction not started")
        return self._stage
