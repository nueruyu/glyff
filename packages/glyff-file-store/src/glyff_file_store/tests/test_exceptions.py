from glyff.exceptions import GlyffError, GlyffException

from glyff_file_store.exceptions import GlyffFileStoreError, InvalidStagedContentError


def test_file_store_errors_share_glyff_base_error_class():
    assert issubclass(GlyffFileStoreError, GlyffError)
    assert issubclass(GlyffFileStoreError, GlyffException)
    assert issubclass(InvalidStagedContentError, GlyffFileStoreError)
