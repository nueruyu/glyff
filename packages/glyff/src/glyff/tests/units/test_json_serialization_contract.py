"""The shipped JSON implementations against the contracts they promise."""

import pytest
from glyff.serialization import JsonArgumentCanonicalizer, JsonSerializer
from glyff.testing import ArgumentCanonicalizerContract, SerializerContract


class TestJsonArgumentCanonicalizerContract(ArgumentCanonicalizerContract):
    @pytest.fixture
    def canonicalizer_factory(self):
        return JsonArgumentCanonicalizer


class TestJsonSerializerContract(SerializerContract):
    @pytest.fixture
    def serializer_factory(self):
        return JsonSerializer
