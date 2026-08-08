"""pytest conformance contract for :class:`~glyff.Serializer`.

Re-exported from :mod:`glyff.testing`, the public entry point.

Only what a serializer must promise for a store to keep results and metadata:
a value declared as a type comes back as that value, the bytes do not depend on
the order a mapping happened to be built in, and a value it cannot carry says so
rather than writing something lossy. Which *types* an implementation accepts is
its own business and is tested beside it.
"""

from __future__ import annotations

import pytest

from .._interfaces import Serializer
from ..exceptions import SerializationError


class Unserializable:
    """A value no serializer can be expected to carry."""


class SerializerContract:
    """Conformance suite for a `Serializer` implementation.

    Subclass it and supply a ``serializer`` fixture.
    """

    @pytest.fixture
    def serializer(self) -> Serializer:
        raise NotImplementedError

    async def test_a_value_survives_a_round_trip(self, serializer: Serializer):
        value = {"key": "value", "num": 123, "flag": True, "items": [1, "a"]}

        assert (
            await serializer.deserialize(await serializer.serialize(value, dict), dict)
            == value
        )

    async def test_a_scalar_survives_a_round_trip(self, serializer: Serializer):
        assert (
            await serializer.deserialize(await serializer.serialize("v", str), str)
            == "v"
        )

    async def test_the_bytes_do_not_depend_on_mapping_order(
        self, serializer: Serializer
    ):
        # Two equal mappings must reach the store as one value; anything else
        # would make a recorded result depend on how it was assembled.
        assert await serializer.serialize({"b": 2, "a": 1}, dict) == (
            await serializer.serialize({"a": 1, "b": 2}, dict)
        )

    async def test_a_value_it_cannot_carry_is_refused(self, serializer: Serializer):
        with pytest.raises(SerializationError):
            await serializer.serialize(Unserializable(), Unserializable)
