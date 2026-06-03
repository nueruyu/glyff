import inspect
import json
from typing import Any, Callable

from ..exceptions import SerializationError
from ..interfaces import ArgsHasher, Serializer
from .helpers import build_hashable_args, default_to_hashable, hash_from_dict


def _json_stable_dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


class JsonSerializer(Serializer):
    """A serializer using only the standard `json` module."""

    def serialize(self, value: Any, type_hint: type) -> bytes:
        try:
            return _json_stable_dumps(value).encode("utf-8")
        except TypeError as e:
            raise SerializationError(
                f"Value of type {value.__class__.__name__} could not be serialized "
                f"to JSON. Original error: {e}"
            ) from e

    def deserialize(self, data: bytes, type_hint: type) -> Any:
        return json.loads(data.decode("utf-8"))


class JsonArgsHasher(ArgsHasher):
    """An ArgsHasher using standard JSON serialization."""

    def hash_args(
        self, func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
    ) -> str:
        func_name = getattr(func, "__qualname__", func.__name__)
        args_dict = build_hashable_args(
            func, sig, args, kwargs, transformer=default_to_hashable
        )
        return hash_from_dict(args_dict, func_name)
