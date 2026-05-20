import hashlib
import inspect
import json
from typing import Any, Callable

from ..interfaces import ArgsHasher, Serializer


def _json_stable_dumps(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


class JsonSerializer(Serializer):
    """A serializer using only the standard `json` module."""

    def serialize(self, value: Any, type_hint: type) -> bytes:
        return _json_stable_dumps(value).encode("utf-8")

    def deserialize(self, data: bytes, type_hint: type) -> Any:
        return json.loads(data.decode("utf-8"))


class JsonArgsHasher(ArgsHasher):
    """An ArgsHasher using standard JSON serialization."""

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
        try:
            stable_repr = _json_stable_dumps(args_dict)
        except TypeError as e:
            raise TypeError(
                f"Arguments to '{func.__name__}' could not be serialized to JSON. "
                "Ensure all arguments are JSON-serializable. "
                f"Original error: {e}"
            ) from e
        hasher = hashlib.sha256()
        hasher.update(stable_repr.encode("utf-8"))
        return hasher.hexdigest()
