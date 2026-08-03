import pytest

from glyff.testing import (
    AppVersionContract,
    DurableBackendContract,
    ExecutionBackendContract,
    TextBackendContract,
)
from glyff_file_store import JsonFileBackend


class TestJsonFileBackendContract(
    ExecutionBackendContract,
    DurableBackendContract,
    TextBackendContract,
    AppVersionContract,
):
    @pytest.fixture
    def backend_factory(self, tmp_path):
        def factory(session_id: str):
            return JsonFileBackend(base_dir=tmp_path, session_id=session_id)

        return factory
