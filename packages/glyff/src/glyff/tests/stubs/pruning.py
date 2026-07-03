"""Reference pruning handler for the test suites.

glyff ships no pruning handler — *when/whether* to delete unreachable records
is a userland policy. This is what userland would write: on completion, delete
the completed call's descendants via the concrete store's ``repository``,
staged into the current transaction.
"""

from __future__ import annotations

from glyff._event_system import EventHandler
from glyff.events import ExecutionCompleted


class PruningEventHandler(EventHandler[ExecutionCompleted]):
    """Deletes the descendant records of a completed execution.

    Opens its own transaction: completion is already durable when this runs, so
    the GC is decoupled from the completion commit.
    """

    async def handle(self, event: ExecutionCompleted) -> None:
        repository = event.context.store.repository  # type: ignore[attr-defined]
        async with event.context.get_transaction_scope():
            descendants = await repository.get_descendants(event.execution_id)
            if descendants:
                await repository.delete_executions(descendants)
