import pytest

from glyff import (
    ArgumentCanonicalizer,
    EventEmitter,
    ExecutionId,
    Serializer,
    SessionId,
)
from glyff._context import Context
from glyff._sequencer import Sequencer
from glyff.serialization import (
    JsonArgumentCanonicalizer,
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
def argument_canonicalizer() -> ArgumentCanonicalizer:
    return JsonArgumentCanonicalizer()


@pytest.fixture
def backend_factory() -> BackendFactory:
    # The contract's factory reopens a store by name, which for an in-process
    # store means handing back the same one.
    stores: dict[str, MemoryBackend] = {}

    def factory(store: str) -> MemoryBackend:
        return stores.setdefault(store, MemoryBackend())

    return factory


@pytest.fixture
def mock_backend() -> StubBackend:
    client = MemoryClient()
    return StubBackend(client=client)


@pytest.fixture
def test_context(
    mock_backend: StubBackend,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer: Serializer,
) -> Context:
    return Context(
        session_id=SessionId("test"),
        backend=mock_backend,
        serializer=serializer,
        sequencer=Sequencer(),
        argument_canonicalizer=argument_canonicalizer,
        event_emitter=EventEmitter([]),
    )
