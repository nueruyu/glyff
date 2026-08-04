from typing import Callable

from glyff import (
    ArgumentCanonicalizer,
    Backend,
    EventEmitter,
    Serializer,
    Session,
    SessionId,
)

BackendFactory = Callable[[str], Backend]


def make_session(
    session_id: str | SessionId,
    backend: Backend,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer: Serializer,
    event_emitter: EventEmitter | None = None,
    app_version: str = "test",
) -> Session:
    return Session(
        id=SessionId(session_id) if isinstance(session_id, str) else session_id,
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
        app_version=app_version,
        event_emitter=event_emitter,
    )
