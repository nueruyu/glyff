from ._context import Context, TransactionScope, reset_context, set_context
from ._event_system import EventEmitter
from ._interfaces import (
    ArgumentCanonicalizer,
    Backend,
    ExecutionRepository,
    Serializer,
    TransactionProvider,
)
from ._sequencer import Sequencer
from .exceptions import AppVersionMismatchError


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

    async def _claim_app_version(self) -> None:
        """Records this session's generation, or refuses to resume another's.

        Checked on entry rather than at the first write: an engraved call is far
        from the mistake it would report, and concurrent branches would race to
        write the first stamp.
        """
        versions = self._backend.app_version
        if versions is None:
            return

        recorded = await versions.read()
        declared = self._app_version

        if declared is None:
            if recorded is not None:
                raise AppVersionMismatchError(
                    f"Session {self._id!r} was written under app_version "
                    f"{recorded!r}, but this process declares none. Opting out "
                    "of the check is not something a deleted argument does."
                )
            return

        if recorded == declared:
            return
        if recorded is not None:
            raise AppVersionMismatchError(
                f"Session {self._id!r} was written under app_version "
                f"{recorded!r}, but this process runs {declared!r}. Migrate the "
                "session forward, pin it to the code that started it, or start "
                "a new one."
            )

        # Unrecorded, including a session started before the application
        # declared a version at all: adopt the one it declares now.
        async with TransactionScope(self._backend.transaction_provider):
            await versions.write(declared)

    async def __aenter__(self) -> "Session":
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
