from typing import Callable

from glyff import (
    ArgsCanonicalizer,
    Backend,
    EventEmitter,
    Serializer,
    Session,
)

BackendFactory = Callable[[str], Backend]


def make_session(
    session_id: str,
    backend: Backend,
    canonicalizer: ArgsCanonicalizer,
    serializer: Serializer,
    event_emitter: EventEmitter | None = None,
) -> Session:
    return Session(
        id=session_id,
        backend=backend,
        serializer=serializer,
        canonicalizer=canonicalizer,
        event_emitter=event_emitter,
    )
