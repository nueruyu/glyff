import inspect
import json
from typing import Any, Callable

from .._interfaces import ArgsCanonicalizer, Serializer
from .._models import CanonicalValue
from ..exceptions import SerializationError, UnserializableArgumentError
from .constants import DEFAULT_ENCODING
from ._utils import (
    OpaquePolicy,
    RaiseOnOpaque,
    bind_args,
    stable_json_dumps,
    to_canonical,
    to_serializable,
)


class JsonSerializer(Serializer):
    """A serializer using only the standard `json` module."""

    def __init__(
        self, indent: int | str | None = None, ensure_ascii: bool = False
    ) -> None:
        self._indent = indent
        self._ensure_ascii = ensure_ascii

    def to_jsonable(self, obj: Any) -> Any:
        """json.dumps default hook. Override to support extra types."""
        return to_serializable(obj)

    def _encode(self, value: Any) -> bytes:
        """Encodes a JSON-ready value to stable JSON bytes."""
        text = stable_json_dumps(
            value,
            default=self.to_jsonable,
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


class JsonArgsCanonicalizer(ArgsCanonicalizer):
    """An ArgsCanonicalizer that normalizes into the JSON data model."""

    def __init__(self, opaque_policy: OpaquePolicy | None = None) -> None:
        # How to represent values with no value representation. Defaults to raising,
        # so distinct instances never silently collide on their class name. Compare
        # to None explicitly: a custom policy may be a falsy object.
        self._opaque_policy = (
            RaiseOnOpaque() if opaque_policy is None else opaque_policy
        )

    def canonicalize_value(self, obj: Any) -> CanonicalValue:
        """Canonicalizes one value. Override to support extra types.

        Passing this as the walk's recursion keeps an override in effect at every
        depth, not just for top-level arguments.
        """
        return to_canonical(obj, self._opaque_policy, self.canonicalize_value)

    def canonicalize_args(
        self, func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
    ) -> CanonicalValue:
        try:
            return self.canonicalize_value(bind_args(sig, args, kwargs))
        except UnserializableArgumentError as e:
            func_name = getattr(func, "__qualname__", func.__name__)
            raise UnserializableArgumentError(
                f"Arguments to '{func_name}' could not be canonicalized. "
                f"Ensure all arguments have a value representation. Original error: {e}"
            ) from e
