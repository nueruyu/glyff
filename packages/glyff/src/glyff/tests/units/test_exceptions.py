from glyff.exceptions import (
    ContextNotSetError,
    ExecutionFailedError,
    GlyffError,
    GlyffException,
    MissingTypeHintError,
    SerializationError,
    TypeHintResolutionError,
    UnserializableArgumentError,
    YieldException,
)


def test_glyff_errors_share_base_error_class():
    error_types = [
        ContextNotSetError,
        ExecutionFailedError,
        MissingTypeHintError,
        SerializationError,
        TypeHintResolutionError,
        UnserializableArgumentError,
    ]

    for error_type in error_types:
        assert issubclass(error_type, GlyffError)
        assert issubclass(error_type, GlyffException)


def test_yield_exception_is_control_signal_not_error():
    assert issubclass(YieldException, GlyffException)
    assert not issubclass(YieldException, GlyffError)


def test_unserializable_argument_error_is_serialization_error():
    assert issubclass(UnserializableArgumentError, SerializationError)
