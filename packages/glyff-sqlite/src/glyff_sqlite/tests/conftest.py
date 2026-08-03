from pathlib import Path

import pytest
from glyff import ArgsCanonicalizer, ExecutionId
from glyff.serialization import JsonArgsCanonicalizer, JsonSerializer
from glyff.testing import make_execution_id
from glyff_sqlite import SQLiteBackend


@pytest.fixture
def base_execution_id() -> ExecutionId:
    return make_execution_id("test_func")


@pytest.fixture
def serializer() -> JsonSerializer:
    return JsonSerializer()


@pytest.fixture
def canonicalizer() -> ArgsCanonicalizer:
    return JsonArgsCanonicalizer()


@pytest.fixture
def backend_factory(tmp_path: Path):
    def factory(session_id: str) -> SQLiteBackend:
        return SQLiteBackend(tmp_path / f"{session_id}.sqlite3")

    return factory
