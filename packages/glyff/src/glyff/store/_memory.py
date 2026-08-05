from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

from .._interfaces import ExecutionRepository, Transaction, TransactionProvider
from .._models import Execution, ExecutionId, ExecutionStatus, SessionId
from ._memory_client import MemoryClient
from .staging import (
    ExecutionKey,
    ExecutionStage,
    ExecutionStaging,
    SaveExecution,
)
from .utils import execution_id_to_path


class _MemoryTransaction(Transaction):
    def __init__(self, client: MemoryClient, staging: ExecutionStaging):
        self._client = client
        self._staging = staging
        self._stage: ExecutionStage | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def begin(self) -> _MemoryTransaction:
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


class MemoryExecutionRepository(ExecutionRepository):
    """In-memory Execution aggregate repository."""

    def __init__(self, client: MemoryClient, staging: ExecutionStaging):
        self._client = client
        self._staging = staging

    async def get(
        self, session_id: SessionId, execution_id: ExecutionId
    ) -> Execution | None:
        stage = self._staging.current()
        mutation = stage.lookup(session_id, execution_id) if stage else None
        if mutation is not None:
            return (
                mutation.snapshot.to_execution()
                if isinstance(mutation, SaveExecution)
                else None
            )

        snapshot = await self._client.read_committed(
            ExecutionKey(session_id, execution_id)
        )
        return None if snapshot is None else snapshot.to_execution()

    async def save(self, session_id: SessionId, execution: Execution) -> None:
        self._staging.require_current().save(session_id, execution)

    async def executions(
        self,
        session_id: SessionId,
        *,
        status: ExecutionStatus | None = None,
        under: ExecutionId | None = None,
    ) -> AsyncIterator[Execution]:
        prefix = execution_id_to_path(under) + "/" if under is not None else ""
        visible = {
            execution_id_to_path(execution_id): snapshot
            for execution_id, snapshot in (
                await self._client.committed_for_session(session_id)
            ).items()
        }

        stage = self._staging.current()
        for key, mutation in (stage.snapshot() if stage else {}).items():
            if key.session_id != session_id:
                continue
            path = execution_id_to_path(key.execution_id)
            if isinstance(mutation, SaveExecution):
                visible[path] = mutation.snapshot
            else:
                visible.pop(path, None)

        for path in sorted(visible):
            if not path.startswith(prefix):
                continue
            execution = visible[path].to_execution()
            if status in (None, execution.status):
                yield execution

    async def delete_many(
        self, session_id: SessionId, execution_ids: Iterable[ExecutionId]
    ) -> None:
        stage = self._staging.require_current()
        for execution_id in execution_ids:
            stage.delete(session_id, execution_id)


class MemoryTransactionProvider(TransactionProvider):
    def __init__(self, client: MemoryClient, staging: ExecutionStaging):
        self._client = client
        self._staging = staging

    async def begin_transaction(self) -> Transaction:
        return await _MemoryTransaction(self._client, self._staging).begin()


class MemoryBackend:
    def __init__(self) -> None:
        client = MemoryClient()
        staging = ExecutionStaging()
        self.repository: ExecutionRepository = MemoryExecutionRepository(
            client, staging
        )
        self.transaction_provider: TransactionProvider = MemoryTransactionProvider(
            client, staging
        )
        self._app_versions: dict[str, str] = {}
        self._claim_lock = asyncio.Lock()

    async def claim_session(self, session_id: SessionId, app_version: str) -> str:
        # Nothing here outlives the process, so the claim only has to hold for
        # as long as the records do.
        async with self._claim_lock:
            return self._app_versions.setdefault(session_id.value, app_version)
