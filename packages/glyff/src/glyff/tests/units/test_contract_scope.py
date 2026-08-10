"""The shared contracts must not ask for more than the interfaces promise.

`glyff.testing` publishes these contracts, so anything they require becomes a
requirement on every third-party implementation. The two below implement their
ABC and nothing else — no opaque-value policy, no sorted output — so a contract
that grew past its interface fails here rather than in someone else's suite.
"""

import json
from typing import Any

import pytest
from glyff import ArgumentCanonicalizer, CanonicalValue, Serializer
from glyff.serialization import Opaque
from glyff.testing import ArgumentCanonicalizerContract, SerializerContract


class BareCanonicalizer(ArgumentCanonicalizer):
    def canonicalize(self, arguments) -> CanonicalValue:
        # `Opaque` is a value the interface names, so writing it out is part of
        # implementing this and not an extra the contract asks for.
        return {name: _plain(value) for name, value in arguments.items()}


def _plain(value: Any) -> Any:
    if isinstance(value, Opaque):
        return {"opaque": _plain(value.value)}
    return value


class BareSerializer(Serializer):
    async def serialize(self, value: Any, type_hint: type) -> bytes:
        return json.dumps(value).encode()

    async def deserialize(self, data: bytes, type_hint: type) -> Any:
        return json.loads(data)


class TestABareCanonicalizerSatisfiesTheContract(ArgumentCanonicalizerContract):
    @pytest.fixture
    def canonicalizer_factory(self):
        return BareCanonicalizer


class TestABareSerializerSatisfiesTheContract(SerializerContract):
    @pytest.fixture
    def serializer_factory(self):
        return BareSerializer
