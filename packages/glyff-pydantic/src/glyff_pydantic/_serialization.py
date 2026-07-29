from __future__ import annotations

import asyncio
import json
from typing import Any

from glyff.exceptions import SerializationError
from glyff import CanonicalValue
from glyff.serialization import JsonArgsCanonicalizer, JsonSerializer
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


class PydanticArgsCanonicalizer(JsonArgsCanonicalizer):
    """An ArgsCanonicalizer that understands Pydantic models."""

    def _canonicalize(self, obj: Any) -> CanonicalValue:
        if isinstance(obj, BaseModel):
            return self._model_to_canonical(obj)
        return super()._canonicalize(obj)

    def _model_to_canonical(self, obj: BaseModel) -> CanonicalValue:
        # Dump to python types, order any sets, then let pydantic_core encode the
        # scalars it knows (datetime, UUID, Decimal) and hand the rest back to the
        # shared walk.
        dumped = self._sort_sets(obj.model_dump(mode="python"))
        return super()._canonicalize(
            to_jsonable_python(dumped, fallback=self._canonicalize)
        )

    def _sort_sets(self, val: Any) -> Any:
        # Sets have to be ordered before pydantic_core sees them: it emits a set in
        # (hash-randomized) iteration order, and by the time the shared walk runs
        # the set has already become a list.
        if isinstance(val, (set, frozenset)):
            items = [self._sort_sets(x) for x in val]
            try:
                return sorted(items)
            except TypeError:
                return sorted(
                    items,
                    key=lambda e: json.dumps(
                        e, sort_keys=True, default=self._canonicalize
                    ),
                )
        if isinstance(val, dict):
            return {k: self._sort_sets(v) for k, v in val.items()}
        if isinstance(val, list):
            return [self._sort_sets(x) for x in val]
        if isinstance(val, tuple):
            return tuple(self._sort_sets(x) for x in val)
        return val
