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
        def factory(session_id: str):
            return SQLiteBackend(
                tmp_path / f"{session_id}.sqlite3", session_id=session_id
            )

        return factory
