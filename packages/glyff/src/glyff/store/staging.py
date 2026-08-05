"""Transaction-local execution mutations, shared by the shipped backends.

Every backend stages the same thing — whole execution aggregates keyed by
session and execution id — even though what it eventually writes differs. This
owns that temporary state so each backend does not redefine the transaction-time
semantics of a repository.

Backend support, not core API: nothing in glyff's own contracts mentions these
types, and a backend is free to stage differently. It is public because the
shipped out-of-tree backends use it, so it carries the same stability promise as
the rest of the supported surface.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from .._models import (
    CanonicalArguments,
    Execution,
    ExecutionId,
    ExecutionStatus,
    Metadata,
    SerializedValue,
    SessionId,
)

__all__ = [
    "DeleteExecution",
    "ExecutionKey",
    "ExecutionMutation",
    "ExecutionSnapshot",
    "ExecutionStage",
    "SaveExecution",
    "StageHandle",
]


@dataclass(frozen=True)
class ExecutionKey:
    session_id: SessionId
    execution_id: ExecutionId


@dataclass(frozen=True)
class ExecutionSnapshot:
    """An immutable copy of an Execution at ``repository.save()`` time."""

    id: ExecutionId
    status: ExecutionStatus
    arguments: bytes
    result: bytes | None
    metadata: tuple[tuple[str, bytes], ...]

    @classmethod
    def from_execution(cls, execution: Execution) -> ExecutionSnapshot:
        # Copy every payload: the annotations say ``bytes``, but a caller can
        # hand over a mutable buffer and the copy is what makes this a snapshot.
        return cls(
            id=execution.id,
            status=execution.status,
            arguments=bytes(execution.arguments.data),
            result=(
                bytes(execution.result.data) if execution.result is not None else None
            ),
            metadata=tuple(
                sorted(
                    (name, bytes(item.value.data))
                    for name, item in execution.metadata.items()
                )
            ),
        )

    def to_execution(self) -> Execution:
        return Execution(
            id=self.id,
            status=self.status,
            arguments=CanonicalArguments(self.arguments),
            result=SerializedValue(self.result) if self.result is not None else None,
            metadata={
                name: Metadata(key=name, value=SerializedValue(value))
                for name, value in self.metadata
            },
        )


@dataclass(frozen=True)
class SaveExecution:
    snapshot: ExecutionSnapshot


@dataclass(frozen=True)
class DeleteExecution:
    pass


ExecutionMutation = SaveExecution | DeleteExecution


class _StageBuffer:
    __slots__ = ("mutations", "sealed")

    def __init__(self) -> None:
        self.mutations: dict[ExecutionKey, ExecutionMutation] = {}
        self.sealed = False


class StageHandle:
    """One open staging scope, to hand back to ``seal`` and ``close``.

    Opaque on purpose: a transaction carries it around, and only the
    ``ExecutionStage`` that issued it looks inside.
    """

    __slots__ = ("_token", "_buffer")

    def __init__(self, token: contextvars.Token, buffer: _StageBuffer) -> None:
        self._token = token
        self._buffer = buffer


class ExecutionStage:
    """The mutations an open transaction has staged but not yet persisted.

    Only the lifetime and contents of a stage: it never commits, rolls back,
    reads persistent state, or knows how a backend stores anything. Scopes nest
    through a ``ContextVar``, so a transaction opened inside another stages
    separately and restores its parent when it closes.
    """

    def __init__(self) -> None:
        self._current: contextvars.ContextVar[_StageBuffer | None] = (
            contextvars.ContextVar("glyff_execution_stage", default=None)
        )

    def begin(self) -> StageHandle:
        buffer = _StageBuffer()
        return StageHandle(self._current.set(buffer), buffer)

    def save(self, session_id: SessionId, execution: Execution) -> None:
        buffer = self._require_writable()
        key = ExecutionKey(session_id, execution.id)
        buffer.mutations[key] = SaveExecution(
            ExecutionSnapshot.from_execution(execution)
        )

    def delete(self, session_id: SessionId, execution_id: ExecutionId) -> None:
        buffer = self._require_writable()
        buffer.mutations[ExecutionKey(session_id, execution_id)] = DeleteExecution()

    def lookup(
        self, session_id: SessionId, execution_id: ExecutionId
    ) -> ExecutionMutation | None:
        buffer = self._current.get()
        if buffer is None:
            return None
        return buffer.mutations.get(ExecutionKey(session_id, execution_id))

    def current_snapshot(self) -> dict[ExecutionKey, ExecutionMutation]:
        """A copy of what is staged now, for a repository to overlay while it
        enumerates without the stage changing underneath it."""
        buffer = self._current.get()
        return {} if buffer is None else dict(buffer.mutations)

    def seal(self, handle: StageHandle) -> dict[ExecutionKey, ExecutionMutation]:
        """Refuses further writes and returns the batch to commit or discard."""
        self._require_current(handle)
        if handle._buffer.sealed:
            raise RuntimeError("Execution stage is already sealed.")
        handle._buffer.sealed = True
        return dict(handle._buffer.mutations)

    def close(self, handle: StageHandle) -> None:
        """Closes this scope and restores the enclosing stage, if any."""
        self._require_current(handle)
        self._current.reset(handle._token)

    def _require_writable(self) -> _StageBuffer:
        buffer = self._current.get()
        if buffer is None:
            raise RuntimeError(
                "Execution repository write attempted outside a transaction."
            )
        if buffer.sealed:
            raise RuntimeError("Execution stage is closing.")
        return buffer

    def _require_current(self, handle: StageHandle) -> None:
        if self._current.get() is not handle._buffer:
            raise RuntimeError("Transaction closed out of order.")
