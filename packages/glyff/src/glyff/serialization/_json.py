import json
from collections.abc import Mapping
from typing import Any

from .._execution import CanonicalArgumentMap, CanonicalValue
from .._interfaces import ArgumentCanonicalizer, Serializer
from ..exceptions import SerializationError
from .constants import DEFAULT_ENCODING
from ._canonicalization import to_canonical
from ._fallback import (
    CanonicalFallbackRepresenter,
    fallback_representer_or_reject,
)
from ._utils import stable_json_dumps, to_serializable


class JsonSerializer(Serializer):
    """A serializer using only the standard `json` module."""

    def __init__(
        self, indent: int | str | None = None, ensure_ascii: bool = False
    ) -> None:
        self._indent = indent
        self._ensure_ascii = ensure_ascii

    def json_default(self, obj: Any) -> Any:
        """json.dumps default hook. Override to support extra types."""
        return to_serializable(obj)

    def _encode(self, value: Any) -> bytes:
        """Encodes a JSON-ready value to stable JSON bytes."""
        text = stable_json_dumps(
            value,
            default=self.json_default,
            indent=self._indent,
            ensure_ascii=self._ensure_ascii,
        )
        return text.encode(DEFAULT_ENCODING)

    async def serialize(self, value: Any, type_hint: type) -> bytes:
        try:
            return self._encode(value)
        except TypeError as e:
            raise SerializationError(
                f"Value of type {value.__class__.__name__} could not be serialized "
                f"to JSON. Original error: {e}"
            ) from e

    async def deserialize(self, data: bytes, type_hint: type) -> Any:
        return json.loads(data.decode(DEFAULT_ENCODING))


class JsonArgumentCanonicalizer(ArgumentCanonicalizer):
    """An ArgumentCanonicalizer that normalizes into the JSON data model."""

    def __init__(
        self,
        fallback_representer: CanonicalFallbackRepresenter | None = None,
    ) -> None:
        self._fallback_representer = fallback_representer_or_reject(
            fallback_representer
        )

    def canonicalize_value(self, obj: Any) -> CanonicalValue:
        """Canonicalizes one value. Override to support extra types.

        Passing this as the walk's recursion keeps an override in effect at every
        depth, not just for top-level arguments.
        """
        return to_canonical(obj, self._fallback_representer, self.canonicalize_value)

    def canonicalize(self, arguments: Mapping[str, Any]) -> CanonicalArgumentMap:
        canonical = self.canonicalize_value(dict(arguments))
        assert isinstance(canonical, dict)
        return canonical
