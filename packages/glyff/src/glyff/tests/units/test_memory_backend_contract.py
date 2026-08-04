import pytest

from glyff.store import MemoryBackend
from glyff.testing import (
    AppVersionContract,
    BinarySafeBackendContract,
    ExecutionBackendContract,
)


class TestMemoryBackendContract(
    ExecutionBackendContract, BinarySafeBackendContract, AppVersionContract
):
    @pytest.fixture
    def backend_factory(self):
        # Reopening by name means handing back the same store, which for an
        # in-process one is the same object.
        stores: dict[str, MemoryBackend] = {}

        def factory(store: str):
            return stores.setdefault(store, MemoryBackend())

        return factory
