from typing import Callable, Protocol

from glyff import (
    ArgsHasher,
    EventEmitter,
    ExecutionRepository,
    Serializer,
    Session,
    TransactionProvider,
)


class Backend(Protocol):
    repository: ExecutionRepository
    transaction_provider: TransactionProvider


BackendFactory = Callable[[str], Backend]


def make_session(
    session_id: str,
    backend: Backend,
    hasher: ArgsHasher,
    serializer: Serializer,
    event_emitter: EventEmitter | None = None,
) -> Session:
    return Session(
        id=session_id,
        repository=backend.repository,
        transaction_provider=backend.transaction_provider,
        serializer=serializer,
        hasher=hasher,
        event_emitter=event_emitter,
    )
