from __future__ import annotations

import asyncio
import json
from typing import Any

from glyff.exceptions import SerializationError
from glyff.serialization import JsonArgsHasher, JsonSerializer
from pydantic import BaseModel, TypeAdapter
from pydantic_core import to_jsonable_python


class PydanticSerializer(JsonSerializer):
    """A serializer implementation using Pydantic and JSON."""

    def __init__(
        self, indent: int | str | None = None, ensure_ascii: bool = True
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


class PydanticArgsHasher(JsonArgsHasher):
    """An ArgsHasher implementation that uses Pydantic-aware JSON serialization."""

    def _to_jsonable(self, obj: Any) -> Any:
        if isinstance(obj, BaseModel):
            return self._model_to_hashable(obj)
        return super()._to_jsonable(obj)

    def _model_to_hashable(self, obj: BaseModel) -> Any:
        # Dump to python types (keeping sets so they can be sorted), then let
        # pydantic_core encode known types and fall back to identity hashing for
        # opaque members.
        dumped = self._canonicalize(obj.model_dump(mode="python"))
        return to_jsonable_python(dumped, fallback=self._to_jsonable)

    def _canonicalize(self, val: Any) -> Any:
        # Replace sets with sorted lists, recursively, for cross-process-stable hashing.
        if isinstance(val, (set, frozenset)):
            items = [self._canonicalize(x) for x in val]
            try:
                return sorted(items)
            except TypeError:
                return sorted(
                    items,
                    key=lambda e: json.dumps(
                        e, sort_keys=True, default=self._to_jsonable
                    ),
                )
        if isinstance(val, dict):
            return {k: self._canonicalize(v) for k, v in val.items()}
        if isinstance(val, list):
            return [self._canonicalize(x) for x in val]
        if isinstance(val, tuple):
            return tuple(self._canonicalize(x) for x in val)
        return val
