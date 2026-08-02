from __future__ import annotations

import asyncio
from typing import Any

from glyff.exceptions import SerializationError
from glyff import CanonicalValue
from glyff.serialization import (
    JsonArgsCanonicalizer,
    JsonSerializer,
    OpaqueContext,
    OpaquePolicy,
    RaiseOnOpaque,
)
from pydantic import BaseModel, TypeAdapter
from pydantic_core import PydanticSerializationError, to_jsonable_python


class PydanticSerializer(JsonSerializer):
    """A serializer implementation using Pydantic and JSON."""

    def __init__(
        self, indent: int | str | None = None, ensure_ascii: bool = False
    ) -> None:
        super().__init__(indent=indent, ensure_ascii=ensure_ascii)
        self._adapters: dict[Any, TypeAdapter] = {}
        self._lock = asyncio.Lock()

    def _to_jsonable(self, obj: Any) -> Any:
        if isinstance(obj, BaseModel):
            return obj.model_dump(mode="json")
        return super()._to_jsonable(obj)

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


class _PydanticScalars(OpaquePolicy):
    """Represents the scalars pydantic knows — datetime, UUID, Decimal — by value.

    They reach a policy because they have no structural representation, which is
    exactly what a policy decides. Tagging their output is right too: a datetime
    and the string spelling of it are different arguments.
    """

    def __init__(self, fallback: OpaquePolicy) -> None:
        self._fallback = fallback

    def represent(self, ctx: OpaqueContext) -> Any:
        try:
            return to_jsonable_python(ctx.value)
        except PydanticSerializationError:
            return self._fallback.represent(ctx)


class PydanticArgsCanonicalizer(JsonArgsCanonicalizer):
    """An ArgsCanonicalizer that understands Pydantic models."""

    def __init__(self, opaque_policy: OpaquePolicy | None = None) -> None:
        fallback = RaiseOnOpaque() if opaque_policy is None else opaque_policy
        super().__init__(_PydanticScalars(fallback))

    def _canonicalize(self, obj: Any) -> CanonicalValue:
        if isinstance(obj, BaseModel):
            # Dump to python types only. The shared walk owns every container from
            # here, so a model's mappings and sets get the same key checks and
            # ordering as anything else; pydantic's own encoder would stringify
            # mapping keys first and collapse two distinct keys into one.
            return super()._canonicalize(obj.model_dump(mode="python"))
        return super()._canonicalize(obj)
