import uuid

import pytest

from glyff import ExecutionId
from glyff.context import Context, TransactionScope
from glyff.event_system import EventEmitter
from glyff.interfaces import ArgsHasher, Serializer, SessionStore
from glyff.sequencer import Sequencer
from glyff.serialization import (
    JsonArgsHasher,
    JsonSerializer,
)
from glyff.stores import MemoryClient, MemorySessionStore
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
        store=mock_store,
        sequencer=Sequencer(),
        hasher=hasher,
        transaction_scope_factory=lambda: TransactionScope(mock_store),
        event_emitter=EventEmitter([]),
    )
