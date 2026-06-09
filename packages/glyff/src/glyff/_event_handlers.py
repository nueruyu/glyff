from __future__ import annotations

from ._event_system import EventHandler
from .events import ExecutionCompleted


class PruningEventHandler(EventHandler[ExecutionCompleted]):
    """Handles pruning of descendant records upon execution completion."""

    async def handle(self, event: ExecutionCompleted) -> None:
        ctx = event.context
        execution_id = event.execution_id

        descendants = await ctx.store.get_descendants(execution_id)
        if descendants:
            await ctx.store.delete_executions(descendants)
