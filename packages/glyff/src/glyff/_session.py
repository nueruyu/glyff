from ._context import Context, reset_context, set_context
from ._event_system import EventEmitter
from ._interfaces import (
    ArgumentCanonicalizer,
    Backend,
    ExecutionRepository,
    Serializer,
    TransactionProvider,
)
from ._sequencer import Sequencer
from .exceptions import AppVersionMismatchError, StoreSessionMismatchError


class Session:
    """
    Manages the lifecycle of a workflow execution.

    It sets up the execution context. Execution records are persisted per
    event, so there is no session-wide transaction to commit at exit.

    ``app_version`` marks the generation of code the session's records belong
    to. Entering a session whose records were written under a different one
    raises :class:`~glyff.exceptions.AppVersionMismatchError` instead of
    replaying them against code that may no longer mean the same thing. Leave it
    unset to opt out; a store that records no version (an ephemeral one) never
    participates.
    """

    def __init__(
        self,
        id: str,
        *,
        backend: Backend,
        serializer: Serializer,
        argument_canonicalizer: ArgumentCanonicalizer,
        app_version: str | None = None,
        event_emitter: EventEmitter | None = None,
    ) -> None:
        self._id = id
        self._backend = backend
        self._argument_canonicalizer = argument_canonicalizer
        self._serializer = serializer
        self._app_version = app_version
        self._event_emitter = event_emitter or EventEmitter([])
        self._context: Context | None = None
        self._context_token = None

    @property
    def id(self) -> str:
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

    def _check_store_scope(self) -> None:
        """Refuses a store claimed by a different session.

        The store is named where it is constructed and this session is named
        here, so the two can disagree. Nothing downstream would notice: the
        records would simply be written into another session's history.
        """
        claimed = self._backend.session_id
        if claimed is not None and claimed != self._id:
            raise StoreSessionMismatchError(
                f"This backend holds session {claimed!r}, but it was given to "
                f"session {self._id!r}. Build the backend for the session that "
                "uses it."
            )

    async def _claim_app_version(self) -> None:
        """Records this session's generation, or refuses to resume another's.

        Checked on entry rather than at the first write: an engraved call is far
        from the mistake it would report.
        """
        version_store = self._backend.app_version_store
        if version_store is None:
            return

        declared = self._app_version
        if declared is None:
            # Nothing to claim, but a store that already carries a version still
            # holds records that belong to it.
            recorded = await version_store.read()
            if recorded is not None:
                raise AppVersionMismatchError(
                    f"Session {self._id!r} was written under app_version "
                    f"{recorded!r}, but this process declares none. Opting out "
                    "of the check is not something a deleted argument does."
                )
            return

        recorded = await version_store.claim(declared)
        if recorded != declared:
            raise AppVersionMismatchError(
                f"Session {self._id!r} was written under app_version "
                f"{recorded!r}, but this process runs {declared!r}. Migrate the "
                "session forward, pin it to the code that started it, or start "
                "a new one."
            )

    async def __aenter__(self) -> "Session":
        self._check_store_scope()
        await self._claim_app_version()
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
