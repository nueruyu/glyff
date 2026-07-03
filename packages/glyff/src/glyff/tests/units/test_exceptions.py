from glyff.exceptions import (
    ContextNotSetError,
    GlyffError,
    GlyffException,
    MissingTypeHintError,
    SerializationError,
    TypeHintResolutionError,
    UnserializableArgumentError,
)


def test_glyff_errors_share_base_error_class():
    error_types = [
        ContextNotSetError,
        MissingTypeHintError,
        SerializationError,
        TypeHintResolutionError,
        UnserializableArgumentError,
    ]

    for error_type in error_types:
        assert issubclass(error_type, GlyffError)
        assert issubclass(error_type, GlyffException)


def test_unserializable_argument_error_is_serialization_error():
    assert issubclass(UnserializableArgumentError, SerializationError)
