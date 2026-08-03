"""Session entry checks the generation of code behind a store's records."""

import pytest
from glyff import AppVersionStore, ArgumentCanonicalizer, Serializer, Session
from glyff.exceptions import AppVersionMismatchError
from glyff.store import MemoryBackend


class FakeAppVersionStore(AppVersionStore):
    def __init__(self, recorded: str | None = None):
        self.recorded = recorded
        self.writes: list[str] = []

    async def read(self) -> str | None:
        return self.recorded

    async def write(self, app_version: str) -> None:
        self.writes.append(app_version)
        self.recorded = app_version


def _session(
    versions: AppVersionStore | None,
    serializer: Serializer,
    argument_canonicalizer: ArgumentCanonicalizer,
    app_version: str | None = None,
) -> Session:
    backend = MemoryBackend()
    backend.app_version = versions
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

    assert versions.writes == ["v1"]


async def test_matching_version_is_not_rewritten(serializer, argument_canonicalizer):
    versions = FakeAppVersionStore("v1")

    async with _session(versions, serializer, argument_canonicalizer, "v1"):
        pass

    assert versions.writes == []


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

    assert versions.writes == []
    assert versions.recorded is None


async def test_store_without_versions_never_participates(
    serializer, argument_canonicalizer
):
    async with _session(None, serializer, argument_canonicalizer, "v1"):
        pass
