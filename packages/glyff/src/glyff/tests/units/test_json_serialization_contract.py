"""The shipped JSON implementations against the contracts they promise."""

import pytest
from glyff.serialization import (
    JsonArgumentCanonicalizer,
    JsonSerializer,
    OpaquePolicy,
)
from glyff.testing import ArgumentCanonicalizerContract, SerializerContract


class TestJsonArgumentCanonicalizerContract(ArgumentCanonicalizerContract):
    @pytest.fixture
    def canonicalizer_factory(self):
        def factory(opaque_policy: OpaquePolicy | None = None):
            return JsonArgumentCanonicalizer(opaque_policy=opaque_policy)

        return factory


class TestJsonSerializerContract(SerializerContract):
    @pytest.fixture
    def serializer(self) -> JsonSerializer:
        return JsonSerializer()
