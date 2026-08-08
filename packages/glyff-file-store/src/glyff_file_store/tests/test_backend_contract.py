import pytest

from glyff.testing import (
    DomainVersionContract,
    DurableBackendContract,
    EngravedCallContract,
    ExecutionBackendContract,
    ParallelContract,
    PruningContract,
    ResumeContract,
    SessionMigrationContract,
    TextBackendContract,
)
from glyff_file_store import JsonFileBackend


class TestJsonFileBackendContract(
    ExecutionBackendContract,
    DurableBackendContract,
    TextBackendContract,
    DomainVersionContract,
    SessionMigrationContract,
    EngravedCallContract,
    ResumeContract,
    ParallelContract,
    PruningContract,
):
    @pytest.fixture
    def backend_factory(self, tmp_path):
        def factory(store: str):
            return JsonFileBackend(base_dir=tmp_path / store)

        return factory
