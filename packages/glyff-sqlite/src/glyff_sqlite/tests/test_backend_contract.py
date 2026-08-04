import pytest

from glyff.testing import (
    AppVersionContract,
    DurableBackendContract,
    ExecutionBackendContract,
    TextBackendContract,
)
from glyff_sqlite import SQLiteBackend


class TestSQLiteBackendContract(
    ExecutionBackendContract,
    DurableBackendContract,
    TextBackendContract,
    AppVersionContract,
):
    @pytest.fixture
    def backend_factory(self, tmp_path):
        def factory(store: str):
            return SQLiteBackend(tmp_path / f"{store}.sqlite3")

        return factory
