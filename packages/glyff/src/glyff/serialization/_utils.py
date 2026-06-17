"""Turn call arguments into stable JSON for hashing and serialization.

Both paths encode dataclasses and JSON-native values by value. They differ only on
objects with no value representation: hashing identifies them by class name, while
serialization raises (its output must round-trip back to the real value).
"""

import dataclasses
import hashlib
import inspect
import json
from typing import Any, Callable

from ..exceptions import UnserializableArgumentError
from .constants import DEFAULT_ENCODING, JSON_SEPARATORS


def _qualified_name(obj: Any) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


def _hashed_fields(obj: Any) -> list[dataclasses.Field]:
    # Exclude fields the dataclass itself excludes from __hash__ (field(compare=False)).
    return [f for f in dataclasses.fields(obj) if (f.hash if f.hash is not None else f.compare)]


def _sorted_for_hash(values: Any) -> list:
    # A set has no stable cross-process order; impose one via each element's canonical
    # JSON (works for unorderable/mixed elements; str()/repr() would be id-based).
    return sorted(values, key=lambda v: stable_json_dumps(v, default=to_hashable))


def to_hashable(obj: Any) -> Any:
    """json.dumps default hook for hashing. Encodes by value, else by class name."""
    if isinstance(obj, type):
        return _qualified_name(obj)
    if dataclasses.is_dataclass(obj):
        return {f.name: getattr(obj, f.name) for f in _hashed_fields(obj)}
    if isinstance(obj, (set, frozenset)):
        return _sorted_for_hash(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if callable(obj) and hasattr(obj, "__qualname__"):
        return _qualified_name(obj)
    return _qualified_name(type(obj))


def to_serializable(obj: Any) -> Any:
    """json.dumps default hook for serialization. Encodes by value, else raises."""
    if isinstance(obj, type):
        return _qualified_name(obj)
    if dataclasses.is_dataclass(obj):
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    if callable(obj) and hasattr(obj, "__qualname__"):
        return _qualified_name(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def stable_json_dumps(
    data: Any,
    default: Callable[[Any], Any] | None = None,
    indent: int | str | None = None,
    ensure_ascii: bool = True,
) -> str:
    """Creates a stable JSON string from arbitrary data."""
    try:
        return json.dumps(
            data,
            indent=indent,
            sort_keys=True,
            ensure_ascii=ensure_ascii,
            default=default or to_serializable,
            separators=JSON_SEPARATORS if indent is None else None,
        )
    except TypeError as e:
        raise UnserializableArgumentError(
            "Value could not be serialized to JSON for hashing. "
            f"Ensure all components are JSON-serializable. Original error: {e}"
        ) from e


def build_hashable_args(
    sig: inspect.Signature, args: tuple, kwargs: dict
) -> dict[str, Any]:
    """Binds arguments into a name->value dict, dropping *args/**kwargs."""
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()

    args_dict: dict[str, Any] = {}
    for name, value in bound.arguments.items():
        if sig.parameters[name].kind not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            args_dict[name] = value
    return args_dict


def hash_from_dict(
    d: dict, func_name: str, default: Callable[[Any], Any] = to_hashable
) -> str:
    """Creates a stable SHA256 hash from a dictionary."""
    try:
        stable_repr = stable_json_dumps(d, default=default)
    except UnserializableArgumentError as e:
        raise UnserializableArgumentError(
            f"Arguments to '{func_name}' could not be serialized to JSON. "
            f"Ensure all arguments are JSON-serializable. Original error: {e}"
        ) from e

    hasher = hashlib.sha256()
    hasher.update(stable_repr.encode(DEFAULT_ENCODING))
    return hasher.hexdigest()
