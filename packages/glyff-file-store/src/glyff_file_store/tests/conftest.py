from pathlib import Path

import pytest
from glyff import ExecutionId
from glyff.interfaces import ArgsHasher, Serializer, SessionStore
from glyff.serialization import JsonArgsHasher, JsonSerializer

from glyff_file_store import FileClient, JsonFileSessionStore


@pytest.fixture
def base_execution_id() -> ExecutionId:
    return ExecutionId(
        parent_id=None, name="test_func", sequence=0, args_hash="abcde123"
    )


@pytest.fixture
def serializer() -> Serializer:
    return JsonSerializer()


@pytest.fixture
def hasher() -> ArgsHasher:
    return JsonArgsHasher()


@pytest.fixture
def store_factory(tmp_path: Path, serializer: Serializer):
    def factory(session_id: str) -> SessionStore:
        client = FileClient(base_dir=tmp_path, session_id=session_id)
        return JsonFileSessionStore(client=client, serializer=serializer)

    return factory
