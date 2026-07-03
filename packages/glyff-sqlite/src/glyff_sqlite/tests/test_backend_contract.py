import pytest

from glyff.tests.contracts.execution_backend_contract import (
    DurableBackendContract,
    ExecutionBackendContract,
)
from glyff_sqlite import SQLiteBackend


class TestSQLiteBackendContract(ExecutionBackendContract, DurableBackendContract):
    @pytest.fixture
    def backend_factory(self, tmp_path):
        def factory(session_id: str):
            return SQLiteBackend(tmp_path / f"{session_id}.sqlite3")

        return factory
