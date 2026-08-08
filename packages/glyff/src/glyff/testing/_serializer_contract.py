"""pytest conformance contract for :class:`~glyff.Serializer`.

Re-exported from :mod:`glyff.testing`, the public entry point.

Only what the interface promises: a value declared as a type comes back as that
value. What the bytes in between look like — whether they are stable across
mapping orders, which values are refused and how — is each implementation's own,
and is proved beside it.
"""

from __future__ import annotations

import pytest

from .._interfaces import Serializer


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
