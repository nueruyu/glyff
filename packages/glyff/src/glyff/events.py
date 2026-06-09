from __future__ import annotations

from dataclasses import dataclass

from ._context import Context
from ._event_system import Event
from ._models import ExecutionId


@dataclass(frozen=True)
class ExecutionCompleted(Event):
    """Event fired when an execution completes successfully."""

    context: Context
    execution_id: ExecutionId


@dataclass(frozen=True)
class ExecutionFailed(Event):
    """Event fired when an execution fails."""

    context: Context
    execution_id: ExecutionId
    error: str


@dataclass(frozen=True)
class ExecutionYielded(Event):
    """Event fired when an execution is gracefully interrupted."""

    context: Context
    execution_id: ExecutionId
