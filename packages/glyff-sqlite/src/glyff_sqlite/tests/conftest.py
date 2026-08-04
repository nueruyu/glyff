from pathlib import Path

import pytest
from glyff import ArgumentCanonicalizer, ExecutionId
from glyff.serialization import JsonArgumentCanonicalizer, JsonSerializer
from glyff.testing import make_execution_id
from glyff_sqlite import SQLiteBackend


@pytest.fixture
def base_execution_id() -> ExecutionId:
    return make_execution_id("test_func")


@pytest.fixture
def serializer() -> JsonSerializer:
    return JsonSerializer()


@pytest.fixture
def argument_canonicalizer() -> ArgumentCanonicalizer:
    return JsonArgumentCanonicalizer()


@pytest.fixture
def backend_factory(tmp_path: Path):
    def factory(store: str) -> SQLiteBackend:
        return SQLiteBackend(tmp_path / f"{store}.sqlite3")

    return factory
