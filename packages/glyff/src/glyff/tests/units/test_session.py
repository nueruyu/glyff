import pytest

from glyff import ArgsHasher, Serializer, Session
from glyff.store import MemoryBackend


def test_session_requires_explicit_keyword_collaborators(
    hasher: ArgsHasher, serializer: Serializer
):
    backend = MemoryBackend()

    session = Session(
        id="id",
        repository=backend.repository,
        transaction_provider=backend.transaction_provider,
        serializer=serializer,
        hasher=hasher,
    )

    assert session.repository is backend.repository
    assert session.transaction_provider is backend.transaction_provider


def test_session_rejects_old_positional_usage(
    hasher: ArgsHasher, serializer: Serializer
):
    backend = MemoryBackend()

    with pytest.raises(TypeError):
        Session("id", backend.repository, hasher)  # type: ignore[call-arg]


def test_session_rejects_store_keyword(hasher: ArgsHasher, serializer: Serializer):
    backend = MemoryBackend()
    kwargs = {
        "store": backend.repository,
        "serializer": serializer,
        "hasher": hasher,
    }

    with pytest.raises(TypeError):
        Session(id="id", **kwargs)  # type: ignore[arg-type]
