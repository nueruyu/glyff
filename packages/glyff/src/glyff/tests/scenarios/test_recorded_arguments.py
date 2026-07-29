import hashlib
import inspect
import json

from glyff import ArgsCanonicalizer, ExecutionId, engrave
from glyff.serialization._utils import args_digest, encode_canonical
from glyff.tests.types import BackendFactory, make_session


@engrave
async def greet(name: str, times: int = 1) -> str:
    return " ".join([f"hello {name}"] * times)


def _expected_id(canonicalizer: ArgsCanonicalizer, *args, **kwargs) -> ExecutionId:
    sig = inspect.signature(greet)
    data = encode_canonical(canonicalizer.canonicalize_args(greet, sig, args, kwargs))
    return ExecutionId(
        parent_id=None,
        name=greet.__qualname__,
        sequence=0,
        args_hash=args_digest(data),
    )


async def test_recorded_args_are_the_digest_preimage(
    backend_factory: BackendFactory, canonicalizer: ArgsCanonicalizer, serializer
):
    backend = backend_factory("recorded-args")
    async with make_session("recorded-args", backend, canonicalizer, serializer):
        await greet("world")

    execution = await backend.repository.get(_expected_id(canonicalizer, "world"))
    assert execution is not None
    # The invariant a migration relies on: an execution's key is recomputable from
    # the bytes it stores, without re-running the canonicalizer over live objects.
    assert execution.id.args_hash == hashlib.sha256(execution.args.data).hexdigest()
    # Defaults participate, so the recorded form shows what the call was keyed by.
    assert json.loads(execution.args.data) == {"name": "world", "times": 1}


async def test_recorded_args_keep_non_ascii_readable(
    backend_factory: BackendFactory, canonicalizer: ArgsCanonicalizer, serializer
):
    backend = backend_factory("recorded-args-unicode")
    async with make_session(
        "recorded-args-unicode", backend, canonicalizer, serializer
    ):
        await greet("世界")

    execution = await backend.repository.get(_expected_id(canonicalizer, "世界"))
    assert execution is not None
    assert execution.id.args_hash == hashlib.sha256(execution.args.data).hexdigest()
    assert "世界".encode() in execution.args.data
