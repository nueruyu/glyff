from pathlib import Path

import pytest
from glyff import ArgumentCanonicalizer, ExecutionId
from glyff.serialization import JsonArgumentCanonicalizer, JsonSerializer

from glyff.testing import make_execution_id
from glyff_file_store import JsonFileBackend


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
    def factory(session_id: str) -> JsonFileBackend:
        return JsonFileBackend(base_dir=tmp_path, session_id=session_id)

    return factory
