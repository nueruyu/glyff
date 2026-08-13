from __future__ import annotations

import contextvars
from collections.abc import Iterator, Sequence
from typing import Any, overload

from ._event_system import EventEmitter
from ._domain_claims import DomainClaims
from ._execution import SerializedValue
from ._types import ExecutionId, SessionId
from ._interfaces import (
    ArgumentCanonicalizer,
    Backend,
    ExecutionRepository,
    Serializer,
    Transaction,
    TransactionProvider,
)
from ._sequencer import Sequencer
from .exceptions import ContextNotSetError, NoCurrentExecutionError


class MetadataAccessor:
    """Provides access to the metadata of the current execution."""

    def __init__(self, ctx: Context):
        self._ctx = ctx

    async def set(self, key: str, value: Any, value_type: type | None = None) -> None:
        """Attach metadata to the current execution, staged into the open
        transaction. ``value_type`` defaults to ``type(value)``; raises
        :class:`NoCurrentExecutionError` outside an engraved call.
        """
        execution_id = self._ctx.tracer.current
        if execution_id is None:
            raise NoCurrentExecutionError(
                "set() requires an active execution; call it from within "
                "an engraved function."
            )
        execution = await self._ctx.repository.get(self._ctx.session_id, execution_id)
        if execution is None:
            raise LookupError(f"Execution {execution_id} not found")

        serialized = await self._ctx.serializer.serialize(
            value,
            type(value) if value_type is None else value_type,
        )
        execution.set_metadata(key, SerializedValue(serialized))
        await self._ctx.repository.save(self._ctx.session_id, execution)

    async def get(
        self,
        key: str,
        return_type: type,
        *,
        execution_id: ExecutionId | None = None,
    ) -> Any | None:
        """Read a per-execution metadata entry, deserialized to ``return_type``.

        Defaults to the current execution; pass ``execution_id`` to read
        another's. Returns ``None`` if the execution or key is absent.
        """
        target = execution_id if execution_id is not None else self._ctx.tracer.current
        if target is None:
            raise NoCurrentExecutionError(
                "get() requires an active execution or an explicit execution_id."
            )
        execution = await self._ctx.repository.get(self._ctx.session_id, target)
        if execution is None:
            return None

        metadata = execution.get_metadata(key)
        if metadata is None:
            return None

        return await self._ctx.serializer.deserialize(metadata.value.data, return_type)


class Context:
    """Holds the execution context for a workflow session."""

    def __init__(
        self,
        session_id: SessionId,
        backend: Backend,
        serializer: Serializer,
        sequencer: Sequencer,
        argument_canonicalizer: ArgumentCanonicalizer,
        event_emitter: EventEmitter,
    ) -> None:
        self._session_id = session_id
        self._repository = backend.repository
        self._transaction_provider = backend.transaction_provider
        self._serializer = serializer
        self._sequencer = sequencer
        # Derived from what the context already holds, so a session's claims can
        # never be made against another session or another store.
        self._domain_claims = DomainClaims(backend=backend, session_id=session_id)
        self._argument_canonicalizer = argument_canonicalizer
        self._event_emitter = event_emitter
        self._tracer = ExecutionTracer()

    @property
    def session_id(self) -> SessionId:
        return self._session_id

    @property
    def repository(self) -> ExecutionRepository:
        return self._repository

    @property
    def serializer(self) -> Serializer:
        return self._serializer

    @property
    def transaction_provider(self) -> TransactionProvider:
        return self._transaction_provider

    @property
    def sequencer(self) -> Sequencer:
        return self._sequencer

    @property
    def domain_claims(self) -> DomainClaims:
        return self._domain_claims

    @property
    def argument_canonicalizer(self) -> ArgumentCanonicalizer:
        return self._argument_canonicalizer

    @property
    def event_emitter(self) -> EventEmitter:
        return self._event_emitter

    @property
    def tracer(self) -> ExecutionTracer:
        return self._tracer

    @property
    def metadata(self) -> MetadataAccessor:
        """Returns an accessor for managing the metadata of the current execution."""
        return MetadataAccessor(self)

    @property
    def call_stack(self) -> CallStack:
        return self._tracer.call_stack

    @property
    def current_execution_id(self) -> ExecutionId | None:
        return self._tracer.current

    def get_transaction_scope(self) -> TransactionScope:
        """Return a fresh transaction scope."""
        return TransactionScope(self._transaction_provider)


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
    """A single-use transaction scope around a TransactionProvider."""

    def __init__(self, transaction_provider: TransactionProvider):
        self._transaction_provider = transaction_provider
        self._transaction: Transaction | None = None
        self._closed = False

    async def __aenter__(self) -> "TransactionScope":
        if self._closed or self._transaction is not None:
            raise RuntimeError("TransactionScope cannot be re-entered.")
        self._transaction = await self._transaction_provider.begin_transaction()
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
