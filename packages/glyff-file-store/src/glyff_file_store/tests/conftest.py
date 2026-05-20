from pathlib import Path

import pytest
from glyff import ExecutionId
from glyff.interfaces import ArgsHasher, Serializer, SessionStore
from glyff.serialization import JsonArgsHasher, JsonSerializer
from pytest import FixtureRequest

from glyff_file_store import FileClient, FileSessionStore


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


@pytest.fixture(params=["file_jsonl", "file_json"])
def store_factory(request: FixtureRequest, tmp_path: Path, serializer: Serializer):
    param = request.param

    def factory(session_id: str) -> SessionStore:
        client = FileClient(base_dir=tmp_path, session_id=session_id)
        fmt = "json" if param == "file_json" else "jsonl"
        return FileSessionStore(client=client, serializer=serializer, format=fmt)

    return factory
