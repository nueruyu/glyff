import inspect
import json
from typing import Any, Callable

from .._interfaces import ArgsHasher, Serializer
from ..exceptions import SerializationError, UnserializableArgumentError
from .constants import DEFAULT_ENCODING
from ._utils import (
    OpaquePolicy,
    RaiseOnOpaque,
    build_hashable_args,
    hash_from_dict,
    stable_json_dumps,
    to_hashable,
    to_serializable,
)


class JsonSerializer(Serializer):
    """A serializer using only the standard `json` module."""

    def __init__(
        self, indent: int | str | None = None, ensure_ascii: bool = True
    ) -> None:
        self._indent = indent
        self._ensure_ascii = ensure_ascii

    def _to_jsonable(self, obj: Any) -> Any:
        """json.dumps default hook. Override to support extra types."""
        return to_serializable(obj)

    def _encode(self, value: Any) -> bytes:
        """Encodes a JSON-ready value to stable JSON bytes."""
        text = stable_json_dumps(
            value,
            default=self._to_jsonable,
            indent=self._indent,
            ensure_ascii=self._ensure_ascii,
        )
        return text.encode(DEFAULT_ENCODING)

    async def serialize(self, value: Any, type_hint: type) -> bytes:
        try:
            return self._encode(value)
        except UnserializableArgumentError as e:
            raise SerializationError(
                f"Value of type {value.__class__.__name__} could not be serialized "
                f"to JSON. Original error: {e}"
            ) from e

    async def deserialize(self, data: bytes, type_hint: type) -> Any:
        return json.loads(data.decode(DEFAULT_ENCODING))


class JsonArgsHasher(ArgsHasher):
    """An ArgsHasher using standard JSON serialization."""

    def __init__(self, opaque_policy: OpaquePolicy | None = None) -> None:
        # How to hash values with no value representation. Defaults to raising, so
        # distinct instances never silently collide on their class name. Compare to
        # None explicitly: a custom policy may be a falsy object.
        self._opaque_policy = (
            RaiseOnOpaque() if opaque_policy is None else opaque_policy
        )

    def _to_jsonable(self, obj: Any) -> Any:
        """json.dumps default hook. Override to support extra types."""
        return to_hashable(obj, self._opaque_policy)

    def hash_args(
        self, func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
    ) -> str:
        func_name = getattr(func, "__qualname__", func.__name__)
        args_dict = build_hashable_args(sig, args, kwargs)
        return hash_from_dict(args_dict, func_name, default=self._to_jsonable)
