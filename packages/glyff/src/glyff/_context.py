from __future__ import annotations

import contextvars
from collections.abc import Iterator, Sequence
from typing import Callable, overload

from ._event_system import EventEmitter
from ._interfaces import ArgsHasher, SessionStore, Transaction
from ._models import ExecutionId
from ._sequencer import Sequencer
from .exceptions import ContextNotSetError


class Context:
    """Holds the execution context for a workflow session."""

    def __init__(
        self,
        session_id: str,
        store: SessionStore,
        sequencer: Sequencer,
        hasher: ArgsHasher,
        transaction_scope_factory: Callable[[], TransactionScope],
        event_emitter: EventEmitter,
    ) -> None:
        self._session_id = session_id
        self._store = store
        self._sequencer = sequencer
        self._hasher = hasher
        self._transaction_scope_factory = transaction_scope_factory
        self._event_emitter = event_emitter
        self._tracer = ExecutionTracer()
        self._current_transaction_scope: TransactionScope | None = None

    @property
    def store(self) -> SessionStore:
        return self._store

    @property
    def sequencer(self) -> Sequencer:
        return self._sequencer

    @property
    def hasher(self) -> ArgsHasher:
        return self._hasher

    @property
    def event_emitter(self) -> EventEmitter:
        return self._event_emitter

    @property
    def tracer(self) -> ExecutionTracer:
        return self._tracer

    @property
    def call_stack(self) -> CallStack:
        return self._tracer.call_stack

    @property
    def current_execution_id(self) -> ExecutionId | None:
        return self._tracer.current

    @property
    def in_transaction(self) -> bool:
        """Returns True if currently within a transaction scope."""
        ts = self._current_transaction_scope
        return ts is not None and ts.in_transaction

    def get_transaction_scope(self) -> TransactionScope:
        if self._current_transaction_scope is None:
            self._current_transaction_scope = self._transaction_scope_factory()
        return self._current_transaction_scope


class CallStack(Sequence[ExecutionId]):
    """Read-only view of the execution call stack. No allocation on access."""

    __slots__ = ("_data",)

    def __init__(self, data: list[ExecutionId]) -> None:
        self._data = data

    @overload
    def __getitem__(self, index: int) -> ExecutionId: ...
    @overload
    def __getitem__(self, index: slice) -> list[ExecutionId]: ...

    def __getitem__(self, index):
        return self._data[index]

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, item: object) -> bool:
        return item in self._data

    def __iter__(self) -> Iterator[ExecutionId]:
        return iter(self._data)

    def __repr__(self) -> str:
        return f"CallStack({self._data!r})"


class ExecutionTracer:
    """Records the active call stack during workflow execution."""

    def __init__(self) -> None:
        self._stack: list[ExecutionId] = []
        self._view = CallStack(self._stack)

    @property
    def call_stack(self) -> CallStack:
        return self._view

    @property
    def current(self) -> ExecutionId | None:
        return self._stack[-1] if self._stack else None

    def start(self, execution_id: ExecutionId) -> None:
        self._stack.append(execution_id)

    def end(self) -> None:
        self._stack.pop()


class TransactionScope:
    """
    Manages a transaction across a SessionStore, supporting nesting.
    The actual commit/rollback only happens at the outermost scope.
    """

    def __init__(self, store: SessionStore):
        self._store = store
        self._level = 0
        self._transaction: Transaction | None = None

    @property
    def in_transaction(self) -> bool:
        """Returns True if currently within a transaction scope."""
        return self._level > 0

    async def __aenter__(self):
        if self._level == 0:
            self._transaction = await self._store.begin_transaction()
        self._level += 1
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._level -= 1
        if self._level == 0 and self._transaction:
            # Commit on success or on any Exception: completed work stays
            # durable and the interrupted call remains retryable. Roll back only
            # on BaseException (KeyboardInterrupt, SystemExit, cancellation).
            if exc_type is None or issubclass(exc_type, Exception):
                await self._transaction.commit()
            else:
                await self._transaction.rollback()


_context_var: contextvars.ContextVar[Context] = contextvars.ContextVar("glyff_context")


def get_context() -> Context:
    """Retrieves the current workflow context."""
    try:
        return _context_var.get()
    except LookupError:
        raise ContextNotSetError(
            "Workflow context is not set. Are you running outside a Session?"
        )


def set_context(ctx: Context) -> contextvars.Token:
    """Sets the current workflow context. Returns a token that can be used to reset it."""
    return _context_var.set(ctx)


def reset_context(token: contextvars.Token) -> None:
    """Resets the workflow context to a previous state using the provided token."""
    _context_var.reset(token)
