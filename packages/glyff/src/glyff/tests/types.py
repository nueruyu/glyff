from typing import Callable

from glyff import (
    ArgsHasher,
    Backend,
    EventEmitter,
    Serializer,
    Session,
)

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
        backend=backend,
        serializer=serializer,
        hasher=hasher,
        event_emitter=event_emitter,
    )
