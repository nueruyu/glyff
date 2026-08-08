"""The Pydantic implementations against the contracts they promise."""

import pytest
from glyff.serialization import OpaquePolicy
from glyff.testing import ArgumentCanonicalizerContract, SerializerContract

from glyff_pydantic import PydanticArgumentCanonicalizer, PydanticSerializer


class TestPydanticArgumentCanonicalizerContract(ArgumentCanonicalizerContract):
    @pytest.fixture
    def canonicalizer_factory(self):
        def factory(opaque_policy: OpaquePolicy | None = None):
            return PydanticArgumentCanonicalizer(opaque_policy=opaque_policy)

        return factory


class TestPydanticSerializerContract(SerializerContract):
    @pytest.fixture
    def serializer(self) -> PydanticSerializer:
        return PydanticSerializer()
