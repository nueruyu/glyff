"""The shared contracts must not ask for more than the interfaces promise.

`glyff.testing` publishes these contracts, so anything they require becomes a
requirement on every third-party implementation. The two below implement their
ABC and nothing else — no opaque-value policy, no sorted output — so a contract
that grew past its interface fails here rather than in someone else's suite.
"""

import json
from typing import Any

import pytest
from glyff import (
    ArgumentCanonicalizer,
    CanonicalArgumentMap,
    Opaque,
    Serializer,
    make_opaque_marker,
    require_unreserved_canonical_mapping,
)
from glyff.testing import ArgumentCanonicalizerContract, SerializerContract


class BareCanonicalizer(ArgumentCanonicalizer):
    def canonicalize(self, arguments) -> CanonicalArgumentMap:
        # The opaque marker is the canonical format's, so writing and reserving
        # it is part of implementing this, not an extra the contract asks for.
        return {name: _plain(value) for name, value in arguments.items()}


def _plain(value: Any) -> Any:
    if isinstance(value, Opaque):
        return make_opaque_marker(_plain(value.representation))
    if isinstance(value, dict):
        require_unreserved_canonical_mapping(value)
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
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
