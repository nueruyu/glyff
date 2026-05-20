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
