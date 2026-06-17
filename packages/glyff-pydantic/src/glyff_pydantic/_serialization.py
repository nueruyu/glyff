from __future__ import annotations

import asyncio
import hashlib
import inspect
from typing import Any, Callable

from glyff import ArgsHasher, Serializer
from glyff.exceptions import SerializationError, UnserializableArgumentError
from glyff.serialization.constants import DEFAULT_ENCODING
from glyff.serialization.utils import (
    build_hashable_args,
    stable_json_dumps,
    to_hashable,
    to_serializable,
)
from pydantic import BaseModel, TypeAdapter
from pydantic_core import to_jsonable_python


def _canonicalize(val: Any) -> Any:
    """Replace sets with sorted lists, recursively, for cross-process-stable hashing."""
    if isinstance(val, (set, frozenset)):
        return sorted(
            (_canonicalize(x) for x in val),
            key=lambda e: stable_json_dumps(e, default=to_hashable),
        )
    if isinstance(val, dict):
        return {k: _canonicalize(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_canonicalize(x) for x in val]
    if isinstance(val, tuple):
        return tuple(_canonicalize(x) for x in val)
    return val


def _model_to_hashable(obj: BaseModel) -> Any:
    # Dump to python types (keeping sets so they can be sorted), then let pydantic_core
    # encode known types and fall back to identity hashing for opaque members.
    dumped = _canonicalize(obj.model_dump(mode="python"))
    return to_jsonable_python(dumped, fallback=to_hashable)


def _serialize_default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    return to_serializable(obj)


def _hash_default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return _model_to_hashable(obj)
    return to_hashable(obj)


class PydanticSerializer(Serializer):
    """A serializer implementation using Pydantic and JSON."""

    def __init__(
        self,
        indent: int | str | None = None,
        ensure_ascii: bool = True,
    ) -> None:
        self._adapters: dict[Any, TypeAdapter] = {}
        self._lock = asyncio.Lock()
        self._indent = indent
        self._ensure_ascii = ensure_ascii

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
            json_compatible = adapter.dump_python(value, mode="json")
            return stable_json_dumps(
                json_compatible,
                indent=self._indent,
                ensure_ascii=self._ensure_ascii,
                default=_serialize_default,
            ).encode(DEFAULT_ENCODING)
        except UnserializableArgumentError as e:
            raise SerializationError(
                f"Value of type {value.__class__.__name__} could not be serialized "
                f"with Pydantic. Original error: {e}"
            ) from e
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


class PydanticArgsHasher(ArgsHasher):
    """An ArgsHasher implementation that uses Pydantic-aware JSON serialization."""

    def hash_args(
        self, func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
    ) -> str:
        func_name = getattr(func, "__qualname__", func.__name__)
        args_dict = build_hashable_args(sig, args, kwargs)
        try:
            stable_repr = stable_json_dumps(args_dict, default=_hash_default)
        except (SerializationError, UnserializableArgumentError) as e:
            raise UnserializableArgumentError(
                f"Arguments to '{func_name}' could not be serialized to JSON. "
                f"Ensure all arguments are JSON-serializable. Original error: {e}"
            ) from e
        hasher = hashlib.sha256()
        hasher.update(stable_repr.encode(DEFAULT_ENCODING))
        return hasher.hexdigest()
