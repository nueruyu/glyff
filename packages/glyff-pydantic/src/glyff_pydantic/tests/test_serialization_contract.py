"""The Pydantic implementations against the contracts they promise."""

import pytest
from glyff.testing import ArgumentCanonicalizerContract, SerializerContract

from glyff_pydantic import PydanticArgumentCanonicalizer, PydanticSerializer


class TestPydanticArgumentCanonicalizerContract(ArgumentCanonicalizerContract):
    @pytest.fixture
    def canonicalizer_factory(self):
        return PydanticArgumentCanonicalizer


class TestPydanticSerializerContract(SerializerContract):
    @pytest.fixture
    def serializer_factory(self):
        return PydanticSerializer
