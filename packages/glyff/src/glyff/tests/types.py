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
    executions: ExecutionRepository
    transactions: TransactionProvider


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
        executions=backend.executions,
        transactions=backend.transactions,
        serializer=serializer,
        hasher=hasher,
        event_emitter=event_emitter,
    )
