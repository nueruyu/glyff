"""Stable JSON serialization helpers."""

import dataclasses
import json
from typing import Any, Callable

from .constants import JSON_SEPARATORS


def _qualified_name(obj: Any) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


def to_serializable(obj: Any) -> Any:
    """json.dumps default hook for serialization. Encodes by value, else raises."""
    if isinstance(obj, type):
        return _qualified_name(obj)
    if dataclasses.is_dataclass(obj):
        return {
            field.name: getattr(obj, field.name) for field in dataclasses.fields(obj)
        }
    if callable(obj) and hasattr(obj, "__qualname__"):
        return _qualified_name(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def stable_json_dumps(
    data: Any,
    default: Callable[[Any], Any] | None = None,
    indent: int | str | None = None,
    ensure_ascii: bool = False,
) -> str:
    """Create a stable JSON string from arbitrary data."""
    return json.dumps(
        data,
        indent=indent,
        sort_keys=True,
        ensure_ascii=ensure_ascii,
        default=default or to_serializable,
        separators=JSON_SEPARATORS if indent is None else None,
    )
