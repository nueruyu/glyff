import inspect
import json
from typing import Any, Callable

from .._interfaces import ArgsHasher, Serializer
from ..exceptions import SerializationError, UnserializableArgumentError
from .constants import DEFAULT_ENCODING
from .utils import (
    build_hashable_args,
    hash_from_dict,
    stable_json_dumps,
)


class JsonSerializer(Serializer):
    """A serializer using only the standard `json` module."""

    async def serialize(self, value: Any, type_hint: type) -> bytes:
        try:
            return stable_json_dumps(value).encode(DEFAULT_ENCODING)
        except UnserializableArgumentError as e:
            raise SerializationError(
                f"Value of type {value.__class__.__name__} could not be serialized "
                f"to JSON. Original error: {e}"
            ) from e

    async def deserialize(self, data: bytes, type_hint: type) -> Any:
        return json.loads(data.decode(DEFAULT_ENCODING))


class JsonArgsHasher(ArgsHasher):
    """An ArgsHasher using standard JSON serialization."""

    def hash_args(
        self, func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
    ) -> str:
        func_name = getattr(func, "__qualname__", func.__name__)
        args_dict = build_hashable_args(sig, args, kwargs)
        return hash_from_dict(args_dict, func_name)
