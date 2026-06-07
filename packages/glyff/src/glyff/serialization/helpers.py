import dataclasses
import hashlib
import inspect
import json
from typing import Any, Callable

from ..exceptions import UnserializableArgumentError
from .constants import DEFAULT_ENCODING, JSON_SEPARATORS


def _qualified_name(obj: Any) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


def default_to_hashable(obj: Any) -> Any:
    """Converts common non-JSON-native objects to a serializable representation."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, type):
        return _qualified_name(obj)
    if callable(obj) and hasattr(obj, "__module__") and hasattr(obj, "__qualname__"):
        return _qualified_name(obj)
    return obj


def default_to_jsonable(obj: Any) -> Any:
    """Converts nested objects encountered by json.dumps to serializable values."""
    converted = default_to_hashable(obj)
    if converted is not obj:
        return converted
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def stable_json_dumps(data: Any, default: Callable[[Any], Any] | None = None) -> str:
    """Creates a stable, compact JSON string from arbitrary data."""
    try:
        return json.dumps(
            data,
            sort_keys=True,
            default=default or default_to_jsonable,
            separators=JSON_SEPARATORS,
        )
    except TypeError as e:
        raise UnserializableArgumentError(
            "Value could not be serialized to JSON for hashing. "
            f"Ensure all components are JSON-serializable. Original error: {e}"
        ) from e


def build_hashable_args(
    func: Callable,
    sig: inspect.Signature,
    args: tuple,
    kwargs: dict,
    transformer: Callable[[Any], Any],
) -> dict[str, Any]:
    """Binds arguments and applies a transformer to each value."""
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()

    args_dict: dict[str, Any] = {}
    for name, value in bound.arguments.items():
        param = sig.parameters[name]
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        args_dict[name] = transformer(value)

    return args_dict


def hash_from_dict(d: dict, func_name: str) -> str:
    """Creates a stable SHA256 hash from a dictionary."""
    try:
        stable_repr = stable_json_dumps(d)
    except UnserializableArgumentError as e:
        raise UnserializableArgumentError(
            f"Arguments to '{func_name}' could not be serialized to JSON. "
            f"Ensure all arguments are JSON-serializable or handled by a custom "
            f"argument transformer. Original error: {e}"
        ) from e

    hasher = hashlib.sha256()
    hasher.update(stable_repr.encode(DEFAULT_ENCODING))
    return hasher.hexdigest()
