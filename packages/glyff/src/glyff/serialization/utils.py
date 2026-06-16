"""Helpers for turning arbitrary call arguments into stable JSON.

Design: "by value" vs "by identity"
------------------------------------
Both hashing and serialization need to map non-JSON-native objects to JSON. The
discriminator is "can we build a *stable serialized value* for this object?":

* JSON-native values and dataclasses are represented *by value* (a dataclass is
  just one type we know how to extract a value from -- see ``default_to_hashable``).
* Everything else is handled *by identity*: hashing falls back to the class'
  qualified name (``default_to_hashable_id``), serialization raises
  (``default_to_jsonable``). See those functions for why the terminal behaviour
  differs.

Python's ``hash()`` / ``__hash__`` is deliberately NOT used as the discriminator:

(a) It is not stable across processes. The resulting ``args_hash`` is a
    cache/resumption key (it becomes part of the file-store path), so it must be
    identical across runs. ``str`` hashing is randomized per-process and the
    default object ``__hash__`` is ``id()``-based -- both change on restart.
(b) It maps the wrong way. A normal ``@dataclass`` (``eq=True``) has
    ``__hash__ is None`` (unhashable) yet we want it by value; a plain object is
    ``id()``-hashable yet we want it identified by class. "Python-hashable" is
    essentially the inverse of the distinction we need.
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


def default_to_hashable(obj: Any) -> Any:
    """Shared value-extraction used by both the hashing and serialization paths.

    Converts the object types we know how to represent *by value* (dataclasses) or
    *by identity* (types, callables) and returns everything else unchanged, leaving
    the caller's terminal json.dumps hook (``default_to_jsonable`` for serialization,
    ``default_to_hashable_id`` for hashing) to decide what to do with it.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        # Shallow field extraction (rather than dataclasses.asdict) so that nested
        # values are handled by json.dumps' default hook instead of being deep-copied.
        # This keeps non-deepcopyable members (locks, sockets, ...) from crashing here
        # and lets nested objects fall back to identity hashing where appropriate.
        return {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    if isinstance(obj, type):
        return _qualified_name(obj)
    if callable(obj) and hasattr(obj, "__module__") and hasattr(obj, "__qualname__"):
        return _qualified_name(obj)
    return obj


def default_to_jsonable(obj: Any) -> Any:
    """json.dumps hook for *serialization*.

    Identical to ``default_to_hashable_id`` except for the terminal fallback: an
    object with no value representation *raises* here, because serialized data is
    read back as the real value (``deserialize``) and a class name cannot be turned
    back into the object. This is the single, intentional divergence between the two
    paths -- it stems from serialization needing to round-trip real data.
    """
    converted = default_to_hashable(obj)
    if converted is not obj:
        return converted
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def default_to_hashable_id(obj: Any) -> Any:
    """json.dumps hook for *hashing*.

    Identical to ``default_to_jsonable`` except for the terminal handling: rather than
    raising, value-bearing types that stdlib json cannot natively encode are hashed by
    their content, and anything still left over is identified by its class' qualified
    name. Hashing only needs a stable fingerprint, so truly opaque values (stateless
    services, tools, ...) are distinguished by their class rather than their state;
    state that should affect the hash must be exposed via a dataclass.
    """
    converted = default_to_hashable(obj)
    if converted is not obj:
        return converted
    # Value types json doesn't encode natively: hash by content so distinct values do
    # not silently collide on a shared class name (only the hashing path does this;
    # serialization stays strict).
    if isinstance(obj, (set, frozenset)):
        try:
            return sorted(obj)
        except TypeError:
            # Unorderable elements: order by a stable serialized form (str() would be
            # id-based and therefore unstable across processes for opaque elements).
            return sorted(
                obj, key=lambda e: stable_json_dumps(e, default=default_to_hashable_id)
            )
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    return _qualified_name(type(obj))


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
            default=default or default_to_jsonable,
            separators=JSON_SEPARATORS if indent is None else None,
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
        stable_repr = stable_json_dumps(d, default=default_to_hashable_id)
    except UnserializableArgumentError as e:
        raise UnserializableArgumentError(
            f"Arguments to '{func_name}' could not be serialized to JSON. "
            f"Ensure all arguments are JSON-serializable or handled by a custom "
            f"argument transformer. Original error: {e}"
        ) from e

    hasher = hashlib.sha256()
    hasher.update(stable_repr.encode(DEFAULT_ENCODING))
    return hasher.hexdigest()
