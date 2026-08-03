import hashlib
import inspect
import json

from glyff import ArgumentCanonicalizer, CanonicalArguments, ExecutionId, engrave
from glyff.serialization._utils import encode_canonical
from glyff.tests.types import BackendFactory, make_session


@engrave
async def greet(name: str, times: int = 1) -> str:
    return " ".join([f"hello {name}"] * times)


def _expected_id(
    argument_canonicalizer: ArgumentCanonicalizer, *args, **kwargs
) -> ExecutionId:
    sig = inspect.signature(greet)
    encoded = CanonicalArguments(
        encode_canonical(argument_canonicalizer.canonicalize(greet, sig, args, kwargs))
    )
    return ExecutionId(
        parent_id=None,
        name=greet.__qualname__,
        sequence=0,
        arguments_digest=encoded.digest,
    )


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

    execution = await backend.repository.get(
        _expected_id(argument_canonicalizer, "world")
    )
    assert execution is not None
    assert (
        execution.id.arguments_digest
        == hashlib.sha256(execution.arguments.data).hexdigest()
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

    execution = await backend.repository.get(
        _expected_id(argument_canonicalizer, "世界")
    )
    assert execution is not None
    assert (
        execution.id.arguments_digest
        == hashlib.sha256(execution.arguments.data).hexdigest()
    )
    assert "世界".encode() in execution.arguments.data
