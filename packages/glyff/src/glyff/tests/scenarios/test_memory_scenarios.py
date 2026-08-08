"""The in-memory backend against the session-level contracts."""

import pytest
from glyff.store import MemoryBackend
from glyff.testing import (
    EngravedCallContract,
    ParallelContract,
    PruningContract,
    ResumeContract,
)


class TestMemoryScenarios(
    EngravedCallContract, ResumeContract, ParallelContract, PruningContract
):
    @pytest.fixture
    def backend_factory(self):
        # Reopening by name means handing back the same store, which for an
        # in-process one is the same object.
        stores: dict[str, MemoryBackend] = {}

        def factory(store: str):
            return stores.setdefault(store, MemoryBackend())

        return factory
