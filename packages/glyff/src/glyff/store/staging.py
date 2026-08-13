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
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .._canonical_arguments import CanonicalArguments
from .._execution import (
    Execution,
    ExecutionStatus,
    Metadata,
    SerializedValue,
)
from .._types import ExecutionId, SessionId

__all__ = [
    "DeleteExecution",
    "ExecutionKey",
    "ExecutionMutation",
    "ExecutionSnapshot",
    "ExecutionStage",
    "ExecutionStaging",
    "SaveExecution",
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
            arguments=CanonicalArguments.from_recorded_bytes(self.arguments),
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


class _StageRegistry:
    """Which stage is open, and how a nested one gives its parent back.

    Private because nesting is staging's own business: a backend only ever holds
    the stage it opened.
    """

    __slots__ = ("_open", "_tokens")

    def __init__(self) -> None:
        self._open: contextvars.ContextVar[ExecutionStage | None] = (
            contextvars.ContextVar("glyff_open_execution_stage", default=None)
        )
        # Keyed by stage rather than kept in a stack, because concurrent tasks
        # nest independently and each has to restore the context it replaced.
        # Weakly, so a stage nobody ever closed is collectable like any other.
        self._tokens: weakref.WeakKeyDictionary[
            ExecutionStage, contextvars.Token[ExecutionStage | None]
        ] = weakref.WeakKeyDictionary()

    def open(self) -> ExecutionStage | None:
        # A context copied while a stage was open still holds it after it
        # closes, and there is no token to restore in that copy. Reporting no
        # open stage is what keeps a rolled-back batch from being read there;
        # it costs that copy the enclosing stage, which nothing needs back.
        stage = self._open.get()
        return None if stage is None or stage.closed else stage

    def enter(self, stage: ExecutionStage) -> None:
        self._tokens[stage] = self._open.set(stage)

    def leave(self, stage: ExecutionStage) -> None:
        # Checked before popping, so a close this stage is not entitled to make
        # leaves it exactly as it was.
        if self._open.get() is not stage or stage not in self._tokens:
            raise RuntimeError("Transaction closed out of order.")
        self._open.reset(self._tokens.pop(stage))


class ExecutionStage:
    """The mutations one open transaction has staged but not yet persisted.

    Only the contents and the lifetime of that batch: it never commits, rolls
    back, reads persistent state, or knows how a backend stores anything.
    Closing it finalizes :attr:`batch` and gives back the stage it was opened
    inside, if any.
    """

    __slots__ = ("_registry", "_mutations", "_batch", "__weakref__")

    def __init__(self, registry: _StageRegistry) -> None:
        self._registry = registry
        self._mutations: dict[ExecutionKey, ExecutionMutation] = {}
        self._batch: Mapping[ExecutionKey, ExecutionMutation] | None = None

    @property
    def closed(self) -> bool:
        """Whether the batch is final. A closed stage is nobody's open stage."""
        return self._batch is not None

    @property
    def batch(self) -> Mapping[ExecutionKey, ExecutionMutation]:
        """What the transaction should commit or discard, once it has closed."""
        if self._batch is None:
            raise RuntimeError("Execution stage is still open.")
        return self._batch

    def save(self, session_id: SessionId, execution: Execution) -> None:
        self._require_open()
        self._mutations[ExecutionKey(session_id, execution.id)] = SaveExecution(
            ExecutionSnapshot.from_execution(execution)
        )

    def delete(self, session_id: SessionId, execution_id: ExecutionId) -> None:
        self._require_open()
        self._mutations[ExecutionKey(session_id, execution_id)] = DeleteExecution()

    def lookup(
        self, session_id: SessionId, execution_id: ExecutionId
    ) -> ExecutionMutation | None:
        return self._mutations.get(ExecutionKey(session_id, execution_id))

    def snapshot(self) -> dict[ExecutionKey, ExecutionMutation]:
        """A copy of what is staged now, for a repository to overlay while it
        enumerates without the stage changing underneath it."""
        return dict(self._mutations)

    def close(self) -> None:
        """Finalizes :attr:`batch` and gives back the enclosing stage, if any."""
        # Leaving first: a close this stage is not entitled to make leaves it
        # open and writable rather than half-finalized.
        self._registry.leave(self)
        self._batch = MappingProxyType(dict(self._mutations))

    def _require_open(self) -> None:
        if self.closed:
            raise RuntimeError("Execution stage is closed.")


class ExecutionStaging:
    """Where a backend's repository and its transactions meet.

    A transaction opens a stage and owns it from there; a repository writes to
    and reads from whichever stage is open around the call, so neither has to
    hold a reference to the other. Stages nest through a ``ContextVar``, so a
    transaction opened inside another stages separately.
    """

    __slots__ = ("_registry",)

    def __init__(self) -> None:
        self._registry = _StageRegistry()

    def begin(self) -> ExecutionStage:
        stage = ExecutionStage(self._registry)
        self._registry.enter(stage)
        return stage

    def current(self) -> ExecutionStage | None:
        """The open stage, or ``None`` outside a transaction."""
        return self._registry.open()

    def require_current(self) -> ExecutionStage:
        """The open stage, for a write that has to be inside a transaction."""
        stage = self._registry.open()
        if stage is None:
            raise RuntimeError(
                "Execution repository write attempted outside a transaction."
            )
        return stage
