from ._context import Context, TransactionScope, reset_context, set_context
from ._event_system import EventEmitter
from ._interfaces import ArgsHasher, SessionStore
from ._sequencer import Sequencer


class Session:
    """
    Manages the lifecycle of a workflow execution.

    It sets up the execution context. Stores persist execution events as they
    happen (per event), so there is no session-wide transaction to commit at
    exit.
    """

    def __init__(
        self,
        id: str,
        store: SessionStore,
        hasher: ArgsHasher,
        event_emitter: EventEmitter | None = None,
    ):
        self._id = id
        self._store = store
        self._hasher = hasher
        self._event_emitter = event_emitter or EventEmitter([])
        self._context: Context | None = None
        self._context_token = None

    @property
    def id(self) -> str:
        """Returns the ID of this Session."""
        return self._id

    @property
    def store(self) -> SessionStore:
        """Returns the SessionStore used by this Session."""
        return self._store

    async def __aenter__(self) -> "Session":
        self._context = Context(
            session_id=self._id,
            store=self._store,
            sequencer=Sequencer(),
            hasher=self._hasher,
            transaction_scope_factory=lambda: TransactionScope(self._store),
            event_emitter=self._event_emitter,
        )
        self._context_token = set_context(self._context)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Execution events are persisted per event by the executor, so there is
        # no session-wide transaction to commit or roll back here.
        if self._context_token:
            reset_context(self._context_token)
