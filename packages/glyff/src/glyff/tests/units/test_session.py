import pytest

from glyff import ArgumentCanonicalizer, Serializer, Session, SessionId, get_context
from glyff.exceptions import ContextNotSetError
from glyff.store import MemoryBackend


async def test_session_enter_sets_current_context(
    argument_canonicalizer: ArgumentCanonicalizer, serializer: Serializer
):
    backend = MemoryBackend()

    async with Session(
        id=SessionId("session"),
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version="test",
    ) as session:
        ctx = get_context()

        assert session.repository is backend.repository
        assert session.transaction_provider is backend.transaction_provider
        assert ctx.session_id == SessionId("session")
        assert ctx.repository is backend.repository
        assert ctx.transaction_provider is backend.transaction_provider
        assert ctx.serializer is serializer
        assert ctx.argument_canonicalizer is argument_canonicalizer


async def test_session_exit_resets_current_context(
    argument_canonicalizer: ArgumentCanonicalizer, serializer: Serializer
):
    backend = MemoryBackend()

    async with Session(
        id=SessionId("session"),
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version="test",
    ):
        assert get_context().session_id == SessionId("session")

    with pytest.raises(ContextNotSetError):
        get_context()


async def test_session_exit_restores_previous_context(
    argument_canonicalizer: ArgumentCanonicalizer, serializer: Serializer
):
    outer_backend = MemoryBackend()
    inner_backend = MemoryBackend()

    async with Session(
        id=SessionId("outer"),
        backend=outer_backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version="test",
    ):
        outer_ctx = get_context()

        async with Session(
            id=SessionId("inner"),
            backend=inner_backend,
            serializer=serializer,
            argument_canonicalizer=argument_canonicalizer,
            app_version="test",
        ):
            inner_ctx = get_context()
            assert inner_ctx is not outer_ctx
            assert inner_ctx.session_id == SessionId("inner")
            assert inner_ctx.repository is inner_backend.repository

        assert get_context() is outer_ctx

    with pytest.raises(ContextNotSetError):
        get_context()
