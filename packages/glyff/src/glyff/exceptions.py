class GlyffException(Exception):
    """
    Base class for all glyff-specific exceptions.
    """

    pass


class GlyffError(GlyffException):
    """
    Base class for glyff errors.
    """

    pass


class TypeHintResolutionError(GlyffError):
    """
    Raised when type hints for an engraved function cannot be resolved.
    """

    pass


class MissingTypeHintError(GlyffError):
    """
    Raised when an engraved function is missing required type hints.
    """

    pass


class ContextNotSetError(GlyffError):
    """
    Raised when a workflow context is required but has not been configured.
    """

    pass


class NoCurrentExecutionError(GlyffError):
    """
    Raised when per-execution metadata is accessed without an active execution
    (for example, calling ``ctx.metadata.set`` outside an engraved call).
    """

    pass


class SerializationError(GlyffError):
    """
    Raised when a serializer cannot convert a value to its wire representation.
    """

    pass


class ArgumentCanonicalizationError(GlyffError):
    """
    Raised when an argument to an engraved function has no deterministic
    canonical form, so the call cannot be given a stable identity.

    A sibling of SerializationError, not a subclass: canonicalizing arguments for
    identity and serializing values for storage fail for different reasons.
    """

    pass


class InvalidExecutionError(GlyffError):
    """
    Raised when an Execution aggregate would violate one of its invariants —
    constructed inconsistently, or rebuilt from a corrupt record.
    """

    pass


class StoreFormatVersionError(GlyffError):
    """
    Raised when a persistent store's on-disk format version is one this build of
    glyff does not understand — an unknown or newer version than it wrote.

    glyff stamps a format version when it first writes a store and refuses to
    operate on a store stamped with any other version, rather than risk
    misreading data written by a different build. There is no migration runner
    yet; the stamp exists so a format change is detected loudly instead of
    corrupting silently.
    """

    pass


class StoreSessionMismatchError(GlyffError):
    """
    Raised when a session-scoped store already holds another session's records.

    A store that keys executions by call path alone has no room for a second
    session: two sessions writing into it would interleave into one history. The
    store records which session claimed it and refuses any other, rather than
    mixing them silently.
    """

    pass


class AppVersionMismatchError(GlyffError):
    """
    Raised when a session's records were written under a different
    ``app_version`` than the one now opening it.

    Recorded results are replayed into the current code, so a generation change
    is refused instead of resumed. Migrate the session forward, pin it to the
    code that started it, or start a new one.
    """

    pass
