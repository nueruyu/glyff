import hashlib
import json

from glyff import (
    ArgumentCanonicalizer,
    ArgumentsDigest,
    Domain,
    DomainId,
    DomainVersion,
    Execution,
    SessionId,
)
from glyff.testing import BackendFactory, make_session

DOMAIN = DomainId("test")
engrave = Domain(DOMAIN, version=DomainVersion("1")).engrave


@engrave
async def greet(name: str, times: int = 1) -> str:
    return " ".join([f"hello {name}"] * times)


async def _only_execution(backend, session_id: str) -> Execution:
    # Read back what was stored rather than rebuilding the key: deriving the
    # expected id through the same adapter production uses would find the record
    # a binding bug wrote just as happily as the right one.
    executions = [e async for e in backend.repository.executions(SessionId(session_id))]
    assert len(executions) == 1
    return executions[0]


async def test_recorded_args_are_the_digest_preimage(
    backend_factory: BackendFactory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer,
):
    backend = backend_factory("recorded-args")
    async with make_session(
        "recorded-args", backend, argument_canonicalizer, serializer
    ):
        await greet("world")

    execution = await _only_execution(backend, "recorded-args")
    assert execution.id.arguments_digest == ArgumentsDigest(
        hashlib.sha256(execution.arguments.data).hexdigest()
    )
    # Defaults participate, so the recorded form shows what the call was keyed by.
    assert json.loads(execution.arguments.data) == {"name": "world", "times": 1}


async def test_recorded_args_keep_non_ascii_readable(
    backend_factory: BackendFactory,
    argument_canonicalizer: ArgumentCanonicalizer,
    serializer,
):
    backend = backend_factory("recorded-args-unicode")
    async with make_session(
        "recorded-args-unicode", backend, argument_canonicalizer, serializer
    ):
        await greet("世界")

    execution = await _only_execution(backend, "recorded-args-unicode")
    assert execution.id.arguments_digest == ArgumentsDigest(
        hashlib.sha256(execution.arguments.data).hexdigest()
    )
    assert "世界".encode() in execution.arguments.data
