from glyff.exceptions import GlyffError


class GlyffFileStoreError(GlyffError):
    """
    Base class for glyff-file-store errors.
    """

    pass


class InvalidStagedContentError(GlyffFileStoreError):
    """
    Raised when staged file content is neither bytes nor a write callback.
    """

    pass
