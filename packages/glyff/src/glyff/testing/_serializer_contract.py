"""pytest conformance contract for :class:`~glyff.Serializer`.

Re-exported from :mod:`glyff.testing`. A value declared as a type comes back as
that value. What the bytes in between look like is each implementation's own.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from .._interfaces import Serializer

SerializerFactory = Callable[[], Serializer]


class SerializerContract:
    """Conformance suite for a `Serializer` implementation.

    Subclass it and supply a ``serializer_factory`` fixture, which builds an
    equivalent serializer each time it is called.
    """

    @pytest.fixture
    def serializer_factory(self) -> SerializerFactory:
        raise NotImplementedError

    @pytest.fixture
    def serializer(self, serializer_factory: SerializerFactory) -> Serializer:
        return serializer_factory()

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

    async def test_a_later_instance_reads_what_an_earlier_one_wrote(
        self, serializer_factory: SerializerFactory
    ):
        # A record outlives the session that wrote it, and a session that resumes
        # a paused run is handed a serializer it built itself — after the record
        # was written, which is the order these two are built in.
        value = {"key": "value", "items": [1, "a"]}

        writer = serializer_factory()
        data = await writer.serialize(value, dict)

        reader = serializer_factory()
        assert await reader.deserialize(data, dict) == value
