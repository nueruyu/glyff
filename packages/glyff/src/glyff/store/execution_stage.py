"""Transaction-local execution mutations, shared by the shipped backends.

Every backend stages the same thing — whole execution aggregates keyed by
session and execution id — even though what it eventually writes differs. This
owns that temporary state so each backend does not redefine the transaction-time
semantics of a repository.
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
        return cls(
            id=execution.id,
            status=execution.status,
            arguments=execution.arguments.data,
            result=execution.result.data if execution.result is not None else None,
            metadata=tuple(
                sorted(
                    (name, item.value.data) for name, item in execution.metadata.items()
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


@dataclass(frozen=True)
class StageHandle:
    token: contextvars.Token
    buffer: _StageBuffer


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
        return StageHandle(token=self._current.set(buffer), buffer=buffer)

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
        if handle.buffer.sealed:
            raise RuntimeError("Execution stage is already sealed.")
        handle.buffer.sealed = True
        return dict(handle.buffer.mutations)

    def close(self, handle: StageHandle) -> None:
        """Closes this scope and restores the enclosing stage, if any."""
        self._require_current(handle)
        self._current.reset(handle.token)

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
        if self._current.get() is not handle.buffer:
            raise RuntimeError("Transaction closed out of order.")
