from __future__ import annotations

import asyncio
import datetime
import decimal
import enum
import ipaddress
import pathlib
import uuid
from typing import Any

from glyff.exceptions import SerializationError
from glyff import CanonicalValue
from glyff.serialization import (
    JsonArgumentCanonicalizer,
    JsonSerializer,
    OpaquePolicy,
    RejectOpaque,
)
from pydantic import BaseModel, TypeAdapter
from pydantic_core import to_jsonable_python


class PydanticSerializer(JsonSerializer):
    """A serializer implementation using Pydantic and JSON."""

    def __init__(
        self, indent: int | str | None = None, ensure_ascii: bool = False
    ) -> None:
        super().__init__(indent=indent, ensure_ascii=ensure_ascii)
        self._adapters: dict[Any, TypeAdapter] = {}
        self._lock = asyncio.Lock()

    def json_default(self, obj: Any) -> Any:
        if isinstance(obj, BaseModel):
            return obj.model_dump(mode="json")
        return super().json_default(obj)

    async def _get_adapter(self, type_hint: type) -> TypeAdapter:
        try:
            if adapter := self._adapters.get(type_hint):
                return adapter
        except TypeError:
            return TypeAdapter(type_hint)

        async with self._lock:
            if adapter := self._adapters.get(type_hint):
                return adapter
            adapter = TypeAdapter(type_hint)
            self._adapters[type_hint] = adapter
            return adapter

    async def serialize(self, value: Any, type_hint: type) -> bytes:
        try:
            adapter = await self._get_adapter(type_hint)
            return self._encode(adapter.dump_python(value, mode="json"))
        except SerializationError:
            raise
        except Exception as e:
            raise SerializationError(
                f"Value of type {value.__class__.__name__} could not be serialized "
                f"with Pydantic. Original error: {e}"
            ) from e

    async def deserialize(self, data: bytes, type_hint: type) -> Any:
        adapter = await self._get_adapter(type_hint)
        return adapter.validate_json(data)


# Types pydantic encodes as a single JSON scalar. Deliberately an allowlist of
# inputs rather than a check on the output: pydantic also converts iterables, and
# handing it one would both walk a container behind the shared canonicalization
# and consume a generator the engraved call has not run yet.
_SCALARS = (
    datetime.date,  # also datetime.datetime
    datetime.time,
    datetime.timedelta,
    decimal.Decimal,
    enum.Enum,
    ipaddress.IPv4Address,
    ipaddress.IPv4Network,
    ipaddress.IPv6Address,
    ipaddress.IPv6Network,
    pathlib.PurePath,
    uuid.UUID,
)


class _PydanticScalarPolicy(OpaquePolicy):
    """Represents the scalar types pydantic knows, deferring everything else."""

    def __init__(self, fallback: OpaquePolicy) -> None:
        self._fallback = fallback

    def represent(self, value: Any) -> Any:
        if isinstance(value, _SCALARS):
            return to_jsonable_python(value)
        return self._fallback.represent(value)


class PydanticArgumentCanonicalizer(JsonArgumentCanonicalizer):
    """An ArgumentCanonicalizer that understands Pydantic models."""

    def __init__(self, opaque_policy: OpaquePolicy | None = None) -> None:
        fallback = RejectOpaque() if opaque_policy is None else opaque_policy
        super().__init__(_PydanticScalarPolicy(fallback))

    def canonicalize_value(self, obj: Any) -> CanonicalValue:
        if isinstance(obj, BaseModel):
            # Keep container traversal in the shared walk, so a model's mapping
            # collision checks and set ordering match every other argument.
            return super().canonicalize_value(obj.model_dump(mode="python"))
        return super().canonicalize_value(obj)
