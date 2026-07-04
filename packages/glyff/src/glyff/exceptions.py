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


class UnserializableArgumentError(SerializationError):
    """
    Raised when an argument to an engraved function cannot be deterministically
    serialized for hashing.
    """

    pass
