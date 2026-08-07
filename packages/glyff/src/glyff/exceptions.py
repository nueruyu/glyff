from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._models import DomainId


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


class DomainVersionMismatchError(GlyffError):
    """
    Raised when a session's records for a domain were written under a different
    version than the one now entering it.

    Recorded results are replayed into the current code, so a generation change
    is refused instead of resumed. Carries the versions so a caller can route
    the session to an offline migration rather than parse the message.
    """

    def __init__(
        self,
        message: str,
        *,
        domain_id: "DomainId",
        recorded_version: str,
        current_version: str,
    ) -> None:
        super().__init__(message)
        self.domain_id = domain_id
        self.recorded_version = recorded_version
        self.current_version = current_version


class MigrationError(GlyffError):
    """
    Base class for failures while carrying a session across a version change.
    """

    pass


class MigrationCollisionError(MigrationError):
    """
    Raised when a migration produces duplicate execution identities.
    """

    pass
