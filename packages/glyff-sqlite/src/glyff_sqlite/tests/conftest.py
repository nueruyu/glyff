from pathlib import Path

import pytest
from glyff import ArgsHasher, ExecutionId
from glyff.serialization import JsonArgsHasher, JsonSerializer

from glyff_sqlite import SQLiteClient, SQLiteSessionStore


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
def store_factory(tmp_path: Path, serializer: JsonSerializer):
    def factory(session_id: str) -> SQLiteSessionStore:
        return SQLiteSessionStore(tmp_path / f"{session_id}.sqlite3", serializer)

    return factory
