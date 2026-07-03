import pytest

from glyff import ArgsHasher, Serializer, Session
from glyff.store import MemoryBackend


def test_session_requires_explicit_keyword_collaborators(
    hasher: ArgsHasher, serializer: Serializer
):
    backend = MemoryBackend()

    session = Session(
        id="id",
        executions=backend.executions,
        transactions=backend.transactions,
        serializer=serializer,
        hasher=hasher,
    )

    assert session.executions is backend.executions
    assert session.transactions is backend.transactions


def test_session_rejects_old_positional_usage(
    hasher: ArgsHasher, serializer: Serializer
):
    backend = MemoryBackend()

    with pytest.raises(TypeError):
        Session("id", backend.executions, hasher)  # type: ignore[call-arg]


def test_session_rejects_store_keyword(hasher: ArgsHasher, serializer: Serializer):
    backend = MemoryBackend()
    kwargs = {
        "store": backend.executions,
        "serializer": serializer,
        "hasher": hasher,
    }

    with pytest.raises(TypeError):
        Session(id="id", **kwargs)  # type: ignore[arg-type]
