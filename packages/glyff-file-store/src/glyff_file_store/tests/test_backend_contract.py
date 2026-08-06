import pytest

from glyff.testing import (
    AppVersionContract,
    DurableBackendContract,
    ExecutionBackendContract,
    SessionMigrationContract,
    TextBackendContract,
)
from glyff_file_store import JsonFileBackend


class TestJsonFileBackendContract(
    ExecutionBackendContract,
    DurableBackendContract,
    TextBackendContract,
    AppVersionContract,
    SessionMigrationContract,
):
    @pytest.fixture
    def backend_factory(self, tmp_path):
        def factory(store: str):
            return JsonFileBackend(base_dir=tmp_path / store)

        return factory
