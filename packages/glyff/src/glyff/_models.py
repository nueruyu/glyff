from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any


@dataclass(frozen=True)
class ExecutionId:
    """
    A unique, deterministic identifier for a task call.
    It forms a hierarchy through the 'parent_id' attribute.
    """

    parent_id: ExecutionId | None
    name: str
    # Ordinal within the (parent_id, name, args_hash) counter scope.
    sequence: int
    args_hash: str

    def __str__(self) -> str:
        """
        Generates a human-readable representation for debugging purposes only.
        This format is NOT guaranteed to be stable or suitable for use as a persistence key.
        """
        parent_info = (
            f", parent='{self.parent_id.name}#{self.parent_id.sequence}'"
            if self.parent_id
            else ""
        )
        return f"ExecutionId(name='{self.name}', sequence={self.sequence}{parent_info})"


class ExecutionStatus(Enum):
    """Represents the lifecycle state of a task."""

    STARTED = auto()
    COMPLETED = auto()
    FAILED = auto()


@dataclass(frozen=True)
class ExecutionRecord:
    """Represents the persisted state and outcome of a single execution."""

    status: ExecutionStatus
    result: Any | None = None
    error: str | None = None
