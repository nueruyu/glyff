"""Session entry checks the generation of code behind a store's records."""

import pytest
from glyff import ArgumentCanonicalizer, Serializer, Session, SessionId
from glyff.exceptions import AppVersionMismatchError
from glyff.store import MemoryBackend

SESSION = SessionId("session")


def _session(
    backend: MemoryBackend,
    serializer: Serializer,
    argument_canonicalizer: ArgumentCanonicalizer,
    app_version: str,
) -> Session:
    return Session(
        id=SESSION,
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version=app_version,
    )


async def test_an_unclaimed_session_takes_the_declared_version(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()

    async with _session(backend, serializer, argument_canonicalizer, "v1"):
        pass

    assert await backend.claim_session(SESSION, "v2") == "v1"


async def test_reentering_under_the_recorded_version_is_accepted(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()

    async with _session(backend, serializer, argument_canonicalizer, "v1"):
        pass
    async with _session(backend, serializer, argument_canonicalizer, "v1"):
        pass


async def test_a_different_version_is_refused(serializer, argument_canonicalizer):
    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer, "v1"):
        pass

    with pytest.raises(AppVersionMismatchError, match="'v1'.*'v2'"):
        async with _session(backend, serializer, argument_canonicalizer, "v2"):
            pass


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

    assert await backend.claim_session(SessionId("orders"), "other") == "v1"
    assert await backend.claim_session(SessionId("refunds"), "other") == "v2"


def test_an_empty_session_id_is_refused():
    with pytest.raises(ValueError):
        SessionId("")


@pytest.mark.parametrize(
    "value", [".", "..", ".hidden", "a/b", "a\\b", "a:b", " padded ", "%2E"]
)
def test_a_path_shaped_session_id_is_still_a_name(value: str):
    # Stores encode the name into whatever their keys allow, so core does not
    # narrow what an application may call its sessions.
    assert SessionId(value).value == value
