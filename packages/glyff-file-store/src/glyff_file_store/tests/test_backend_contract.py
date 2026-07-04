import pytest

from glyff.tests.contracts.execution_backend_contract import (
    DurableBackendContract,
    ExecutionBackendContract,
    TextBackendContract,
)
from glyff_file_store import JsonFileBackend


class TestJsonFileBackendContract(
    ExecutionBackendContract, DurableBackendContract, TextBackendContract
):
    @pytest.fixture
    def backend_factory(self, tmp_path):
        def factory(session_id: str):
            return JsonFileBackend(base_dir=tmp_path, session_id=session_id)

        return factory
