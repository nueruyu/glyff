"""The Pydantic implementations against the contracts they promise."""

import pytest
from glyff.testing import ArgumentCanonicalizerContract, SerializerContract

from glyff_pydantic import PydanticArgumentCanonicalizer, PydanticSerializer


class TestPydanticArgumentCanonicalizerContract(ArgumentCanonicalizerContract):
    @pytest.fixture
    def canonicalizer(self) -> PydanticArgumentCanonicalizer:
        return PydanticArgumentCanonicalizer()


class TestPydanticSerializerContract(SerializerContract):
    @pytest.fixture
    def serializer(self) -> PydanticSerializer:
        return PydanticSerializer()
