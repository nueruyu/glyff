import uuid

import pytest

from glyff import ArgsHasher, EventEmitter, ExecutionId, Serializer, SessionStore
from glyff._context import Context
from glyff._sequencer import Sequencer
from glyff.serialization import (
    JsonArgsHasher,
    JsonSerializer,
)
from glyff.store import MemorySessionStore
from glyff.store._memory_client import MemoryClient
from glyff.tests.stubs.store import StubSessionStore
from glyff.tests.types import StoreFactory


@pytest.fixture
def base_execution_id() -> ExecutionId:
    return ExecutionId(
        parent_id=None, name="test_func", sequence=0, args_hash="abcde123"
    )


@pytest.fixture
def nested_execution_id(base_execution_id: ExecutionId) -> ExecutionId:
    return ExecutionId(
        parent_id=base_execution_id,
        name="nested_func",
        sequence=0,
        args_hash="fghij456",
    )


@pytest.fixture
def serializer() -> Serializer:
    return JsonSerializer()


@pytest.fixture
def hasher() -> ArgsHasher:
    return JsonArgsHasher()


@pytest.fixture
def store_factory(serializer: Serializer) -> StoreFactory:
    def factory(session_id: str) -> SessionStore:
        client = MemoryClient()
        return MemorySessionStore(client=client, serializer=serializer)

    return factory


@pytest.fixture
def mock_store(serializer: Serializer) -> StubSessionStore:
    client = MemoryClient()
    return StubSessionStore(client=client, serializer=serializer)


@pytest.fixture
def test_context(mock_store: StubSessionStore, hasher: ArgsHasher) -> Context:
    return Context(
        session_id=str(uuid.uuid4()),
        executions=mock_store,
        transactions=mock_store,
        serializer=mock_store.serializer,
        sequencer=Sequencer(),
        hasher=hasher,
        event_emitter=EventEmitter([]),
    )
