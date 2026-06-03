import dataclasses
import hashlib
import inspect
import json
from typing import Any, Callable

from ..exceptions import UnserializableArgumentError


def default_to_hashable(obj: Any) -> Any:
    """Converts common non-JSON-native objects to a serializable representation."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, type):
        return f"{obj.__module__}.{obj.__qualname__}"
    return obj


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
        stable_repr = json.dumps(d, sort_keys=True, separators=(",", ":"))
    except TypeError as e:
        raise UnserializableArgumentError(
            f"Arguments to '{func_name}' could not be serialized to JSON. "
            f"Ensure all arguments are JSON-serializable or handled by a custom "
            f"argument transformer. Original error: {e}"
        ) from e

    hasher = hashlib.sha256()
    hasher.update(stable_repr.encode("utf-8"))
    return hasher.hexdigest()
