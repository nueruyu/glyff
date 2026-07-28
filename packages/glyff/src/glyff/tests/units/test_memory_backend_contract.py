import pytest

from glyff.store import MemoryBackend
from glyff.testing import (
    BinarySafeBackendContract,
    ExecutionBackendContract,
)


class TestMemoryBackendContract(ExecutionBackendContract, BinarySafeBackendContract):
    @pytest.fixture
    def backend_factory(self):
        def factory(session_id: str):
            return MemoryBackend()

        return factory
