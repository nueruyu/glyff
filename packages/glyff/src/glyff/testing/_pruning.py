"""Reference pruning handler.

glyff ships no pruning handler — *when/whether* to delete unreachable records
is a userland policy. This is what userland would write: on completion, delete
the completed call's descendants via the context execution repository, staged
into the current transaction.
"""

from __future__ import annotations

from glyff import ExecutionRepository
from glyff._event_system import EventHandler
from glyff.events import ExecutionCompleted


class PruningEventHandler(EventHandler[ExecutionCompleted]):
    """Deletes the descendant records of a completed execution.

    Opens its own transaction: completion is already durable when this runs, so
    the GC is decoupled from the completion commit.
    """

    def __init__(self, repository: ExecutionRepository):
        self._repository = repository

    async def handle(self, event: ExecutionCompleted) -> None:
        async with event.context.get_transaction_scope():
            descendants = await self._repository.descendants_of(event.execution_id)
            if descendants:
                await self._repository.delete_many(descendants)
