import hashlib
import inspect
import json
from typing import Any, Callable

from ..interfaces import ArgsHasher, Serializer
from .helpers import build_hashable_args


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
        args_dict = build_hashable_args(func, sig, args, kwargs)
        try:
            stable_repr = _json_stable_dumps(args_dict)
        except TypeError as e:
            raise TypeError(
                f"Arguments to '{func.__qualname__}' could not be serialized to JSON. "
                "Ensure all arguments and the value from '__glyff_identity__' "
                f"are JSON-serializable. Original error: {e}"
            ) from e
        hasher = hashlib.sha256()
        hasher.update(stable_repr.encode("utf-8"))
        return hasher.hexdigest()
