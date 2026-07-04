import pytest

from glyff.store import MemoryBackend
from glyff.tests.contracts.execution_backend_contract import (
    BinarySafeBackendContract,
    ExecutionBackendContract,
)


class TestMemoryBackendContract(ExecutionBackendContract, BinarySafeBackendContract):
    @pytest.fixture
    def backend_factory(self):
        def factory(session_id: str):
            return MemoryBackend()

        return factory
