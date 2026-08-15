"""The shared contracts must not ask for more than the interfaces promise.

`glyff.testing` publishes these contracts, so anything they require becomes a
requirement on every third-party implementation. The two below implement their
ABC and nothing else — no fallback representer, no sorted output — so a contract
that grew past its interface fails here rather than in someone else's suite.
"""

import json
from collections.abc import Mapping
from typing import Any, cast

import pytest
from glyff import (
    ArgumentCanonicalizer,
    CanonicalArguments,
    CanonicalFallback,
    Serializer,
)
from glyff.testing import ArgumentCanonicalizerContract, SerializerContract


class BareCanonicalizer(ArgumentCanonicalizer):
    def canonicalize(self, arguments: Mapping[str, Any]) -> CanonicalArguments:
        return CanonicalArguments(
            {name: _plain(value) for name, value in arguments.items()}
        )


def _plain(value: Any) -> Any:
    if isinstance(value, CanonicalFallback):
        return value
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in cast(dict[Any, Any], value).items()}
    if isinstance(value, list):
        return [_plain(item) for item in cast(list[Any], value)]
    return value


class BareSerializer(Serializer):
    async def serialize(self, value: Any, type_hint: type[Any]) -> bytes:
        return json.dumps(value).encode()

    async def deserialize(self, data: bytes, type_hint: type[Any]) -> Any:
        return json.loads(data)


class TestABareCanonicalizerSatisfiesTheContract(ArgumentCanonicalizerContract):
    @pytest.fixture
    def canonicalizer_factory(self):
        return BareCanonicalizer


class TestABareSerializerSatisfiesTheContract(SerializerContract):
    @pytest.fixture
    def serializer_factory(self):
        return BareSerializer
