from glyff.exceptions import (
    ContextNotSetError,
    GlyffError,
    GlyffException,
    MissingTypeHintError,
    SerializationError,
    TypeHintResolutionError,
    ArgumentCanonicalizationError,
)


def test_glyff_errors_share_base_error_class():
    error_types = [
        ContextNotSetError,
        MissingTypeHintError,
        SerializationError,
        TypeHintResolutionError,
        ArgumentCanonicalizationError,
    ]

    for error_type in error_types:
        assert issubclass(error_type, GlyffError)
        assert issubclass(error_type, GlyffException)


def test_canonicalization_error_is_not_a_serialization_error():
    # Sibling, not subclass: catching serializer failures must not swallow an
    # argument that has no stable identity.
    assert issubclass(ArgumentCanonicalizationError, GlyffError)
    assert not issubclass(ArgumentCanonicalizationError, SerializationError)
