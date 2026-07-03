import uuid

import pytest

from glyff import ArgsHasher, EventEmitter, ExecutionId, Serializer
from glyff._context import Context
from glyff._sequencer import Sequencer
from glyff.serialization import (
    JsonArgsHasher,
    JsonSerializer,
)
from glyff.store import MemoryBackend
from glyff.store._memory_client import MemoryClient
from glyff.tests.stubs.store import StubBackend
from glyff.tests.types import BackendFactory


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
def backend_factory() -> BackendFactory:
    def factory(session_id: str) -> MemoryBackend:
        return MemoryBackend()

    return factory


@pytest.fixture
def mock_backend() -> StubBackend:
    client = MemoryClient()
    return StubBackend(client=client)


@pytest.fixture
def test_context(
    mock_backend: StubBackend, hasher: ArgsHasher, serializer: Serializer
) -> Context:
    return Context(
        session_id=str(uuid.uuid4()),
        executions=mock_backend.executions,
        transactions=mock_backend.transactions,
        serializer=serializer,
        sequencer=Sequencer(),
        hasher=hasher,
        event_emitter=EventEmitter([]),
    )
