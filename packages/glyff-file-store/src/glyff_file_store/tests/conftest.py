from pathlib import Path

import pytest
from glyff import ArgsHasher, ExecutionId
from glyff.serialization import JsonArgsHasher, JsonSerializer

from glyff_file_store import JsonFileBackend


@pytest.fixture
def base_execution_id() -> ExecutionId:
    return ExecutionId(
        parent_id=None, name="test_func", sequence=0, args_hash="abcde123"
    )


@pytest.fixture
def serializer() -> JsonSerializer:
    return JsonSerializer()


@pytest.fixture
def hasher() -> ArgsHasher:
    return JsonArgsHasher()


@pytest.fixture
def backend_factory(tmp_path: Path):
    def factory(session_id: str) -> JsonFileBackend:
        return JsonFileBackend(base_dir=tmp_path, session_id=session_id)

    return factory
