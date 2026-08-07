from ._context import Context, reset_context, set_context
from ._event_system import EventEmitter
from ._interfaces import (
    ArgumentCanonicalizer,
    Backend,
    ExecutionRepository,
    Serializer,
    TransactionProvider,
)
from ._models import SessionId
from ._sequencer import Sequencer


class Session:
    """
    Manages the lifecycle of a workflow execution.

    It sets up the execution context. Execution records are persisted per
    event, so there is no session-wide transaction to commit at exit.

    A session takes no version of its own. Generations belong to
    :class:`~glyff.Domain`, and one is claimed or verified the first time this
    session enters a function that belongs to it.
    """

    def __init__(
        self,
        id: SessionId,
        *,
        backend: Backend,
        serializer: Serializer,
        argument_canonicalizer: ArgumentCanonicalizer,
        event_emitter: EventEmitter | None = None,
    ) -> None:
        self._id = id
        self._backend = backend
        self._argument_canonicalizer = argument_canonicalizer
        self._serializer = serializer
        self._event_emitter = event_emitter or EventEmitter([])
        self._context: Context | None = None
        self._context_token = None

    @property
    def id(self) -> SessionId:
        """Returns the ID of this Session."""
        return self._id

    @property
    def repository(self) -> ExecutionRepository:
        """Returns the ExecutionRepository used by this Session."""
        return self._backend.repository

    @property
    def transaction_provider(self) -> TransactionProvider:
        """Returns the TransactionProvider used by this Session."""
        return self._backend.transaction_provider

    async def __aenter__(self) -> "Session":
        self._context = Context(
            session_id=self._id,
            backend=self._backend,
            serializer=self._serializer,
            sequencer=Sequencer(),
            argument_canonicalizer=self._argument_canonicalizer,
            event_emitter=self._event_emitter,
        )
        self._context_token = set_context(self._context)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Execution events are persisted per event by the executor, so there is
        # no session-wide transaction to commit or roll back here.
        if self._context_token:
            reset_context(self._context_token)
