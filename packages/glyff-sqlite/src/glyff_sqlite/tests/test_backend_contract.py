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
from glyff_sqlite import SQLiteBackend


class TestSQLiteBackendContract(
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
            return SQLiteBackend(tmp_path / f"{store}.sqlite3")

        return factory
