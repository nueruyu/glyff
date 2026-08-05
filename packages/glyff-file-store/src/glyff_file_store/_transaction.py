from __future__ import annotations

import asyncio

from glyff import Transaction
from glyff.store._execution_stage import ExecutionStage, StageHandle

from glyff_file_store._file_client import FileClient


class _ClientTransaction(Transaction):
    def __init__(self, client: FileClient, stage: ExecutionStage) -> None:
        self._client = client
        self._stage = stage
        self._handle: StageHandle | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def begin(self) -> _ClientTransaction:
        self._handle = self._stage.begin()
        return self

    async def commit(self) -> None:
        async with self._lock:
            if self._closed:
                return
            handle = self._require_handle()
            mutations = self._stage.seal(handle)
            self._closed = True
            try:
                await self._client.commit_mutations(mutations)
            finally:
                self._stage.close(handle)

    async def rollback(self) -> None:
        async with self._lock:
            if self._closed:
                return
            handle = self._require_handle()
            self._stage.seal(handle)
            self._closed = True
            self._stage.close(handle)

    def _require_handle(self) -> StageHandle:
        if self._handle is None:
            raise RuntimeError("transaction not started")
        return self._handle
