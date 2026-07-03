import pytest

from glyff.store import MemoryBackend
from glyff.tests.contracts.execution_backend_contract import ExecutionBackendContract


class TestMemoryBackendContract(ExecutionBackendContract):
    @pytest.fixture
    def backend_factory(self):
        def factory(session_id: str):
            return MemoryBackend()

        return factory
