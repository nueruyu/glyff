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
    default_to_hashable,
    default_to_hashable_id,
    default_to_jsonable,
    stable_json_dumps,
)
from pydantic import BaseModel, TypeAdapter


def _json_default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    try:
        return default_to_jsonable(obj)
    except TypeError:
        raise SerializationError(
            f"Object of type {obj.__class__.__name__} is not JSON serializable"
        )


def _hash_default(obj: Any) -> Any:
    """json.dumps hook for hashing: like ``_json_default`` but identifies otherwise
    unserializable objects by their class' qualified name instead of raising."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    return default_to_hashable_id(obj)


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
                default=_json_default,
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

    def _to_hashable(self, obj: Any) -> Any:
        if isinstance(obj, BaseModel):
            return obj.model_dump(mode="json")
        return default_to_hashable(obj)

    def hash_args(
        self, func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
    ) -> str:
        func_name = getattr(func, "__qualname__", func.__name__)
        args_dict = build_hashable_args(
            func, sig, args, kwargs, transformer=self._to_hashable
        )
        try:
            stable_repr = stable_json_dumps(args_dict, default=_hash_default)
        except (SerializationError, UnserializableArgumentError) as e:
            raise UnserializableArgumentError(
                f"Arguments to '{func_name}' could not be serialized to JSON. "
                f"Ensure all arguments are JSON-serializable or handled by a custom "
                f"argument transformer. Original error: {e}"
            ) from e
        hasher = hashlib.sha256()
        hasher.update(stable_repr.encode(DEFAULT_ENCODING))
        return hasher.hexdigest()
