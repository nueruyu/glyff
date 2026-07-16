"""Turn call arguments into stable JSON for hashing and serialization.

Both paths encode dataclasses and JSON-native values by value. They differ only on
objects with no value representation ("opaque" values): serialization always raises
(its output must round-trip back to the real value), while hashing defers to a
pluggable :class:`OpaquePolicy`. The default hashing policy also raises, so distinct
instances can never silently collide on their class name; callers who want the older
"identify by class" behaviour opt in with :class:`QualnameOpaque` (or their own
policy).
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


def _qualified_name(obj: Any) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


@dataclasses.dataclass(frozen=True)
class OpaqueContext:
    """The context handed to an :class:`OpaquePolicy` for a single opaque value.

    A value is "opaque" when the hasher has no value representation for it: it is not
    a dataclass, set, bytes, type, or qualified callable. Only ``value`` is populated
    today; this object exists so the policy signature can gain fields later without
    breaking existing implementations.
    """

    value: Any


class OpaquePolicy(ABC):
    """Decides how an opaque value contributes to an argument hash.

    glyff owns the hashing *contract* (encode by value, otherwise defer here) but not
    the taxonomy of what counts as opaque or how it is marked -- that is the caller's
    responsibility. Inject a custom policy to recognise your own markers (a decorator,
    base class, attribute, or set of types) and decide how each matched value folds
    into the hash.
    """

    @abstractmethod
    def hash(self, ctx: OpaqueContext) -> Any:
        """Return a JSON-encodable representation of ``ctx.value``, or raise.

        Raising rejects the value (the hash fails loud); returning a value folds that
        representation into the hash.
        """
        ...


class RaiseOnOpaque(OpaquePolicy):
    """Default policy: refuse to hash opaque values.

    Hashing an opaque value by its class name would let distinct instances collide,
    silently handing one call's memoised result to another. This policy fails loud
    instead, so such a collision can never happen by accident.
    """

    def hash(self, ctx: OpaqueContext) -> Any:
        value = ctx.value
        raise UnserializableArgumentError(
            f"Cannot hash opaque value of type '{type(value).__name__}': it has no "
            "value representation. Hashing it by class name would let distinct "
            "instances collide. Give it a serializable representation (e.g. a "
            "dataclass or model), or pass an opaque policy to the hasher."
        )


class QualnameOpaque(OpaquePolicy):
    """Opt-in policy: identify an opaque value by its class' qualified name.

    Collapses every instance of a class to a single hash, so calls that differ only in
    such a value share one memoisation entry. This is safe *only* when the value is
    stateless with respect to the result (e.g. a service holding injected
    dependencies). Opt in explicitly per hasher; it is not the default precisely
    because it hashes distinct instances identically.
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
    return policy.hash(OpaqueContext(value=obj))


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
