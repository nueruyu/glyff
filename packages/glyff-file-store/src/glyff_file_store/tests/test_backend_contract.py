import pytest
from pathlib import Path

from glyff.testing import (
    BackendFactory,
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
    def backend_factory(self, tmp_path: Path) -> BackendFactory:
        def factory(store: str) -> JsonFileBackend:
            return JsonFileBackend(base_dir=tmp_path / store)

        return factory
