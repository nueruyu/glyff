"""Session entry checks the generation of code behind a store's records."""

import pytest
from glyff import AppVersionStore, ArgumentCanonicalizer, Serializer, Session
from glyff.exceptions import AppVersionMismatchError, StoreSessionMismatchError
from glyff.store import MemoryBackend


class FakeAppVersionStore(AppVersionStore):
    """The atomicity the real stores provide, standing in for their storage."""

    def __init__(self, recorded: str | None = None):
        self.recorded = recorded
        self.claims: list[str] = []

    async def read(self) -> str | None:
        return self.recorded

    async def claim(self, app_version: str) -> str:
        self.claims.append(app_version)
        if self.recorded is None:
            self.recorded = app_version
        return self.recorded

    async def write(self, app_version: str) -> None:
        self.recorded = app_version


def _session(
    version_store: AppVersionStore | None,
    serializer: Serializer,
    argument_canonicalizer: ArgumentCanonicalizer,
    app_version: str | None = None,
    session_id: str | None = None,
) -> Session:
    backend = MemoryBackend()
    backend.session_id = session_id
    backend.app_version_store = version_store
    return Session(
        id="session",
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version=app_version,
    )


async def test_unrecorded_store_adopts_the_declared_version(
    serializer, argument_canonicalizer
):
    versions = FakeAppVersionStore()

    async with _session(versions, serializer, argument_canonicalizer, "v1"):
        pass

    assert versions.recorded == "v1"


async def test_matching_version_is_not_rewritten(serializer, argument_canonicalizer):
    versions = FakeAppVersionStore("v1")

    async with _session(versions, serializer, argument_canonicalizer, "v1"):
        pass

    assert versions.recorded == "v1"


async def test_different_version_is_refused(serializer, argument_canonicalizer):
    versions = FakeAppVersionStore("v1")

    with pytest.raises(AppVersionMismatchError, match="'v1'.*'v2'"):
        async with _session(versions, serializer, argument_canonicalizer, "v2"):
            pass

    assert versions.recorded == "v1"


async def test_dropping_the_declaration_is_refused(serializer, argument_canonicalizer):
    # The records still belong to a generation, so silently opting out of the
    # check would resume them under whatever code is running now.
    versions = FakeAppVersionStore("v1")

    with pytest.raises(AppVersionMismatchError):
        async with _session(versions, serializer, argument_canonicalizer):
            pass


async def test_undeclared_version_records_nothing(serializer, argument_canonicalizer):
    versions = FakeAppVersionStore()

    async with _session(versions, serializer, argument_canonicalizer):
        pass

    assert versions.claims == []
    assert versions.recorded is None


async def test_store_without_versions_never_participates(
    serializer, argument_canonicalizer
):
    async with _session(None, serializer, argument_canonicalizer, "v1"):
        pass


async def test_a_store_claimed_by_another_session_is_refused(
    serializer, argument_canonicalizer
):
    # The backend is named where it is built, this session where it is opened,
    # so a typo in either puts one session's records in the other's history.
    with pytest.raises(StoreSessionMismatchError, match="'other'.*'session'"):
        async with _session(
            None, serializer, argument_canonicalizer, session_id="other"
        ):
            pass


async def test_a_store_claimed_by_this_session_is_accepted(
    serializer, argument_canonicalizer
):
    async with _session(None, serializer, argument_canonicalizer, session_id="session"):
        pass
