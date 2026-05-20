from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any, Callable

from glyff.interfaces import ArgsHasher, Serializer
from pydantic import BaseModel, TypeAdapter


def _json_default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    try:
        return json.JSONEncoder().default(obj)
    except TypeError:
        raise TypeError(
            f"Object of type {obj.__class__.__name__} is not JSON serializable"
        )


def _json_stable_dumps(data: Any) -> str:
    return json.dumps(
        data, sort_keys=True, default=_json_default, separators=(",", ":")
    )


class PydanticSerializer(Serializer):
    """A serializer implementation using Pydantic and JSON."""

    def serialize(self, value: Any, type_hint: type) -> bytes:
        adapter = TypeAdapter(type_hint)
        json_compatible = adapter.dump_python(value, mode="json")
        return _json_stable_dumps(json_compatible).encode("utf-8")

    def deserialize(self, data: bytes, type_hint: type) -> Any:
        adapter = TypeAdapter(type_hint)
        return adapter.validate_json(data)


class PydanticArgsHasher(ArgsHasher):
    """An ArgsHasher implementation that uses Pydantic-aware JSON serialization."""

    def hash_args(
        self, func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
    ) -> str:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        args_dict = {
            name: value
            for name, value in bound.arguments.items()
            if sig.parameters[name].kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            and name not in ("self", "cls")
        }
        stable_repr = _json_stable_dumps(args_dict)
        hasher = hashlib.sha256()
        hasher.update(stable_repr.encode("utf-8"))
        return hasher.hexdigest()
