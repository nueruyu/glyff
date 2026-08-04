"""Session entry checks the generation of code behind a store's records."""

import pytest
from glyff import ArgumentCanonicalizer, Serializer, Session, SessionId
from glyff.exceptions import AppVersionMismatchError
from glyff.store import MemoryBackend


def _session(
    backend: MemoryBackend,
    serializer: Serializer,
    argument_canonicalizer: ArgumentCanonicalizer,
    app_version: str | None = None,
) -> Session:
    return Session(
        id=SessionId("session"),
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version=app_version,
    )


async def test_unclaimed_session_adopts_the_declared_version(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()

    async with _session(backend, serializer, argument_canonicalizer, "v1"):
        pass

    assert await backend.claim_session(SessionId("session"), None) == "v1"


async def test_reentering_under_the_recorded_version_is_accepted(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()

    async with _session(backend, serializer, argument_canonicalizer, "v1"):
        pass
    async with _session(backend, serializer, argument_canonicalizer, "v1"):
        pass


async def test_different_version_is_refused(serializer, argument_canonicalizer):
    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer, "v1"):
        pass

    with pytest.raises(AppVersionMismatchError, match="'v1'.*'v2'"):
        async with _session(backend, serializer, argument_canonicalizer, "v2"):
            pass


async def test_dropping_the_declaration_is_refused(serializer, argument_canonicalizer):
    # The records still belong to a generation, so silently opting out of the
    # check would resume them under whatever code is running now.
    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer, "v1"):
        pass

    with pytest.raises(AppVersionMismatchError, match="declares none"):
        async with _session(backend, serializer, argument_canonicalizer):
            pass


async def test_undeclared_version_records_nothing(serializer, argument_canonicalizer):
    backend = MemoryBackend()

    async with _session(backend, serializer, argument_canonicalizer):
        pass

    assert await backend.claim_session(SessionId("session"), None) is None


async def test_sessions_in_one_backend_carry_their_own_versions(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()

    for session_id, app_version in (("orders", "v1"), ("refunds", "v2")):
        async with Session(
            id=SessionId(session_id),
            backend=backend,
            serializer=serializer,
            argument_canonicalizer=argument_canonicalizer,
            app_version=app_version,
        ):
            pass

    assert await backend.claim_session(SessionId("orders"), None) == "v1"
    assert await backend.claim_session(SessionId("refunds"), None) == "v2"


@pytest.mark.parametrize(
    "value", ["", ".", "..", ".hidden", "a/b", "a\\b", "a:b", " padded "]
)
def test_a_session_id_that_could_escape_its_store_is_refused(value: str):
    with pytest.raises(ValueError):
        SessionId(value)
