class InvalidStagedContentError(TypeError):
    """
    Raised when staged file content is neither bytes nor a write callback.
    """

    pass
