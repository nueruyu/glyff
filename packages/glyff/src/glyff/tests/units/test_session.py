import pytest

from glyff import ArgsCanonicalizer, Serializer, Session, get_context
from glyff.exceptions import ContextNotSetError
from glyff.store import MemoryBackend


async def test_session_enter_sets_current_context(
    canonicalizer: ArgsCanonicalizer, serializer: Serializer
):
    backend = MemoryBackend()

    async with Session(
        id="session",
        backend=backend,
        serializer=serializer,
        canonicalizer=canonicalizer,
    ) as session:
        ctx = get_context()

        assert session.repository is backend.repository
        assert session.transaction_provider is backend.transaction_provider
        assert ctx.session_id == "session"
        assert ctx.repository is backend.repository
        assert ctx.transaction_provider is backend.transaction_provider
        assert ctx.serializer is serializer
        assert ctx.canonicalizer is canonicalizer


async def test_session_exit_resets_current_context(
    canonicalizer: ArgsCanonicalizer, serializer: Serializer
):
    backend = MemoryBackend()

    async with Session(
        id="session",
        backend=backend,
        serializer=serializer,
        canonicalizer=canonicalizer,
    ):
        assert get_context().session_id == "session"

    with pytest.raises(ContextNotSetError):
        get_context()


async def test_session_exit_restores_previous_context(
    canonicalizer: ArgsCanonicalizer, serializer: Serializer
):
    outer_backend = MemoryBackend()
    inner_backend = MemoryBackend()

    async with Session(
        id="outer",
        backend=outer_backend,
        serializer=serializer,
        canonicalizer=canonicalizer,
    ):
        outer_ctx = get_context()

        async with Session(
            id="inner",
            backend=inner_backend,
            serializer=serializer,
            canonicalizer=canonicalizer,
        ):
            inner_ctx = get_context()
            assert inner_ctx is not outer_ctx
            assert inner_ctx.session_id == "inner"
            assert inner_ctx.repository is inner_backend.repository

        assert get_context() is outer_ctx

    with pytest.raises(ContextNotSetError):
        get_context()
