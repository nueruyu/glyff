class YieldException(Exception):
    """
    A special exception to signal that the session should be interrupted gracefully.
    This is not an error, but a signal to stop processing and engrave the state.
    """

    pass


class ExecutionFailedError(Exception):
    """
    Raised when attempting to execute a task that has previously failed
    and its failure state is engraved.
    """

    pass


class TypeHintResolutionError(TypeError):
    """
    Raised when type hints for an engraved function cannot be resolved.
    """

    pass


class ContextNotSetError(RuntimeError):
    """
    Raised when a workflow context is required but has not been configured.
    """

    pass


class SerializationError(TypeError):
    """
    Raised when a serializer cannot convert a value to its wire representation.
    """

    pass


class UnserializableArgumentError(TypeError):
    """
    Raised when an argument to an engraved function cannot be deterministically
    serialized for hashing.
    """

    pass
