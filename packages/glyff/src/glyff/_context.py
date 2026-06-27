from __future__ import annotations

import contextvars
from collections.abc import Iterator, Sequence
from typing import overload

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
        event_emitter: EventEmitter,
    ) -> None:
        self._session_id = session_id
        self._store = store
        self._sequencer = sequencer
        self._hasher = hasher
        self._event_emitter = event_emitter
        self._tracer = ExecutionTracer()

    @property
    def session_id(self) -> str:
        return self._session_id

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

    def get_transaction_scope(self) -> TransactionScope:
        """Return a fresh transaction scope.

        The executor opens independent scopes for START, the function body,
        and COMPLETE so each durable boundary can commit or roll back on its
        own.
        """
        return TransactionScope(self._store)


class CallStack(Sequence[ExecutionId]):
    """Read-only wrapper over a call-stack snapshot.

    Holds the underlying sequence by reference (no copy); only the lightweight
    wrapper itself is allocated on access.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Sequence[ExecutionId]) -> None:
        self._data = data

    @overload
    def __getitem__(self, index: int) -> ExecutionId: ...
    @overload
    def __getitem__(self, index: slice) -> Sequence[ExecutionId]: ...

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
    """Records the active call stack during workflow execution.

    The stack lives in a ``ContextVar`` holding an immutable tuple, so
    concurrent tasks (parallel ``asyncio.gather`` branches) each see their own
    call stack: a child spawned under a gather inherits the parent's stack
    snapshot, but its own pushes do not leak to siblings. This keeps parent-id
    resolution correct under parallel execution.
    """

    def __init__(self) -> None:
        self._stack: contextvars.ContextVar[tuple[ExecutionId, ...]] = (
            contextvars.ContextVar("glyff_call_stack", default=())
        )

    @property
    def call_stack(self) -> CallStack:
        return CallStack(self._stack.get())

    @property
    def current(self) -> ExecutionId | None:
        stack = self._stack.get()
        return stack[-1] if stack else None

    def start(self, execution_id: ExecutionId) -> None:
        self._stack.set((*self._stack.get(), execution_id))

    def end(self) -> None:
        self._stack.set(self._stack.get()[:-1])


class TransactionScope:
    """A single-use transaction scope around a SessionStore."""

    def __init__(self, store: SessionStore):
        self._store = store
        self._transaction: Transaction | None = None
        self._closed = False

    async def __aenter__(self) -> "TransactionScope":
        if self._closed or self._transaction is not None:
            raise RuntimeError("TransactionScope cannot be re-entered.")
        self._transaction = await self._store.begin_transaction()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._transaction is None:
            return False
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()
        return False

    async def commit(self) -> None:
        transaction = self._take_transaction()
        await transaction.commit()

    async def rollback(self) -> None:
        transaction = self._take_transaction()
        await transaction.rollback()

    def _take_transaction(self) -> Transaction:
        if self._closed or self._transaction is None:
            raise RuntimeError("TransactionScope is already closed.")
        transaction, self._transaction = self._transaction, None
        self._closed = True
        return transaction


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
