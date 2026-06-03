from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any, Callable

from glyff.exceptions import SerializationError, UnserializableArgumentError
from glyff.interfaces import ArgsHasher, Serializer
from glyff.serialization.helpers import (
    build_hashable_args,
    default_to_hashable,
)
from pydantic import BaseModel, TypeAdapter


def _json_default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    try:
        return json.JSONEncoder().default(obj)
    except TypeError:
        raise SerializationError(
            f"Object of type {obj.__class__.__name__} is not JSON serializable"
        )


def _json_stable_dumps(data: Any) -> str:
    try:
        return json.dumps(
            data, sort_keys=True, default=_json_default, separators=(",", ":")
        )
    except SerializationError:
        raise
    except TypeError as e:
        raise SerializationError(
            f"Value could not be serialized to JSON. Original error: {e}"
        ) from e


class PydanticSerializer(Serializer):
    """A serializer implementation using Pydantic and JSON."""

    def serialize(self, value: Any, type_hint: type) -> bytes:
        try:
            adapter = TypeAdapter(type_hint)
            json_compatible = adapter.dump_python(value, mode="json")
            return _json_stable_dumps(json_compatible).encode("utf-8")
        except SerializationError:
            raise
        except Exception as e:
            raise SerializationError(
                f"Value of type {value.__class__.__name__} could not be serialized "
                f"with Pydantic. Original error: {e}"
            ) from e

    def deserialize(self, data: bytes, type_hint: type) -> Any:
        adapter = TypeAdapter(type_hint)
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
            stable_repr = _json_stable_dumps(args_dict)
        except SerializationError as e:
            raise UnserializableArgumentError(
                f"Arguments to '{func_name}' could not be serialized to JSON. "
                f"Ensure all arguments are JSON-serializable or handled by a custom "
                f"argument transformer. Original error: {e}"
            ) from e
        hasher = hashlib.sha256()
        hasher.update(stable_repr.encode("utf-8"))
        return hasher.hexdigest()
