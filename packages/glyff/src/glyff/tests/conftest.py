import uuid

import pytest

from glyff import ArgsCanonicalizer, EventEmitter, ExecutionId, Serializer
from glyff._context import Context
from glyff._sequencer import Sequencer
from glyff.serialization import (
    JsonArgsCanonicalizer,
    JsonSerializer,
)
from glyff.store import MemoryBackend
from glyff.testing import make_execution_id
from glyff.store._memory_client import MemoryClient
from glyff.tests.stubs.store import StubBackend
from glyff.tests.types import BackendFactory


@pytest.fixture
def base_execution_id() -> ExecutionId:
    return make_execution_id("test_func")


@pytest.fixture
def nested_execution_id(base_execution_id: ExecutionId) -> ExecutionId:
    return make_execution_id("nested_func", parent=base_execution_id)


@pytest.fixture
def serializer() -> Serializer:
    return JsonSerializer()


@pytest.fixture
def canonicalizer() -> ArgsCanonicalizer:
    return JsonArgsCanonicalizer()


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
    mock_backend: StubBackend, canonicalizer: ArgsCanonicalizer, serializer: Serializer
) -> Context:
    return Context(
        session_id=str(uuid.uuid4()),
        backend=mock_backend,
        serializer=serializer,
        sequencer=Sequencer(),
        canonicalizer=canonicalizer,
        event_emitter=EventEmitter([]),
    )
