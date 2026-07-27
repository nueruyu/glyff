"""Turn call arguments into stable JSON for hashing and serialization.

Both paths encode dataclasses and JSON-native values by value. They differ on values
with no value representation ("opaque" values): serialization raises, while hashing
defers to a pluggable :class:`OpaquePolicy` (the default policy raises too).
"""

import dataclasses
import functools
import hashlib
import inspect
import json
from abc import ABC, abstractmethod
from typing import Any, Callable

from ..exceptions import UnserializableArgumentError
from .constants import DEFAULT_ENCODING, JSON_SEPARATORS

# Namespaces an OpaquePolicy's return value so it cannot be confused with a native
# encoding (a policy that returned the string "pkg.Cls" must not hash-equal a plain
# "pkg.Cls" argument).
_OPAQUE_TAG = "__glyff_opaque__"


def _qualified_name(obj: Any) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


@dataclasses.dataclass(frozen=True)
class OpaqueContext:
    """The context handed to an :class:`OpaquePolicy` for a single opaque value.

    Only ``value`` is populated today; the object exists so the policy signature can
    gain fields (e.g. parameter name, function) without breaking implementations. See
    the standard-policies follow-up for the planned additions.
    """

    value: Any


class OpaquePolicy(ABC):
    """Decides how a value with no value representation contributes to a hash.

    glyff owns the hashing contract (encode by value, else defer here); it does not own
    the taxonomy of what is opaque or how it is marked. Inject a policy to recognise
    your own markers and turn each matched value into a hashable representation.
    """

    @abstractmethod
    def hash(self, ctx: OpaqueContext) -> Any:
        """Return a JSON-encodable representation of ``ctx.value``, or raise to reject it."""
        ...


class RaiseOnOpaque(OpaquePolicy):
    """Default policy: reject opaque values so distinct instances never silently collide."""

    def hash(self, ctx: OpaqueContext) -> Any:
        value = ctx.value
        raise UnserializableArgumentError(
            f"Cannot hash opaque value of type '{type(value).__name__}': it has no "
            "value representation. Give it a serializable representation (e.g. a "
            "dataclass or model), or pass an opaque policy to the hasher."
        )


class QualnameOpaque(OpaquePolicy):
    """Opt-in policy: identify an opaque value by its class' qualified name.

    Collapses every instance of a class to one hash. Safe only when the value is
    stateless with respect to the result (e.g. a service holding injected
    dependencies), since distinct instances hash identically.
    """

    def hash(self, ctx: OpaqueContext) -> Any:
        return _qualified_name(type(ctx.value))


# Shared, stateless singleton used as the default wherever a policy is optional.
_DEFAULT_OPAQUE_POLICY: OpaquePolicy = RaiseOnOpaque()


def _hashed_fields(obj: Any) -> list[dataclasses.Field]:
    # Exclude fields the dataclass itself excludes from __hash__ (field(compare=False)).
    return [
        f
        for f in dataclasses.fields(obj)
        if (f.hash if f.hash is not None else f.compare)
    ]


def _sorted_for_hash(
    values: Any, policy: OpaquePolicy = _DEFAULT_OPAQUE_POLICY
) -> list:
    # set/frozenset only define a partial order (subset), so sorted() would silently
    # keep incomparable elements in their (process-randomized) input order. Use the
    # canonical-JSON key whenever an element is a set/frozenset; otherwise sort
    # directly (fast) and fall back to that key for unorderable/mixed elements.
    if not any(isinstance(v, (set, frozenset)) for v in values):
        try:
            return sorted(values)
        except TypeError:
            pass
    key = functools.partial(to_hashable, policy=policy)
    return sorted(values, key=lambda v: stable_json_dumps(v, default=key))


def to_hashable(obj: Any, policy: OpaquePolicy = _DEFAULT_OPAQUE_POLICY) -> Any:
    """json.dumps default hook for hashing. Encodes by value, else defers to policy."""
    if isinstance(obj, type):
        return _qualified_name(obj)
    if isinstance(obj, functools.partial):
        # partials are callable but have no __qualname__; hash by their components.
        return {
            "__partial__": to_hashable(obj.func, policy),
            "args": obj.args,
            "keywords": obj.keywords,
        }
    if dataclasses.is_dataclass(obj):
        return {f.name: getattr(obj, f.name) for f in _hashed_fields(obj)}
    if isinstance(obj, (set, frozenset)):
        return _sorted_for_hash(obj, policy)
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if callable(obj) and hasattr(obj, "__qualname__"):
        return _qualified_name(obj)
    # Tag the policy's output so an opaque value can never hash-collide with a native
    # value that happens to share the policy's representation.
    return {_OPAQUE_TAG: policy.hash(OpaqueContext(value=obj))}


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
    """Binds arguments into a name->value dict, including *args/**kwargs.

    Variadic parameters appear as a tuple (var-positional) and dict (var-keyword),
    both of which json serializes, so they contribute to the hash.
    """
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


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
