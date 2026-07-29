"""Canonicalize call arguments for identity, and serialize values for storage.

Two separate paths that must not be confused. **Canonicalization** normalizes a
call's arguments into the JSON data model so the result depends only on the
argument values — it is one-way, deliberately lossy (it drops what identity does
not depend on) and defers values with no value representation to a pluggable
:class:`OpaquePolicy`. **Serialization** encodes results and metadata faithfully
and raises on anything it cannot represent.

Canonicalization is deliberately split from encoding: :func:`to_canonical`
produces a canonical structure and :func:`encode_canonical` turns it into the
bytes that get hashed *and* stored, so those two can never drift apart.
"""

import dataclasses
import functools
import hashlib
import inspect
import json
import math
from abc import ABC, abstractmethod
from typing import Any, Callable, TypeAlias

from .._models import CanonicalValue
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
    """Decides how a value with no value representation contributes to identity.

    glyff owns the canonicalization contract (encode by value, else defer here); it
    does not own the taxonomy of what is opaque or how it is marked. Inject a policy
    to recognise your own markers and turn each matched value into a canonical
    representation.
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
            f"Cannot canonicalize opaque value of type '{type(value).__name__}': it "
            "has no value representation. Give it a serializable representation "
            "(e.g. a dataclass or model), or pass an opaque policy to the "
            "canonicalizer."
        )


class QualnameOpaque(OpaquePolicy):
    """Opt-in policy: identify an opaque value by its class' qualified name.

    Collapses every instance of a class to one representation. Safe only when the
    value is stateless with respect to the result (e.g. a service holding injected
    dependencies), since distinct instances then canonicalize identically.
    """

    def hash(self, ctx: OpaqueContext) -> Any:
        return _qualified_name(type(ctx.value))


# Shared, stateless singleton used as the default wherever a policy is optional.
_DEFAULT_OPAQUE_POLICY: OpaquePolicy = RaiseOnOpaque()

_Recurse: TypeAlias = Callable[[Any], "CanonicalValue"]


def _hashed_fields(obj: Any) -> list[dataclasses.Field]:
    # Exclude fields the dataclass itself excludes from __hash__ (field(compare=False)).
    return [
        f
        for f in dataclasses.fields(obj)
        if (f.hash if f.hash is not None else f.compare)
    ]


def _canonical_key(key: Any) -> str:
    # Coerce keys the way json renders them, but do it *here* rather than leaving it
    # to the encoder: json orders by the original key, so {2: .., 10: ..} encoded as
    # {"2": .., "10": ..} and re-encoding what you read back reordered it. Recorded
    # arguments have to survive that round trip byte-for-byte, or a migration cannot
    # recompute the key it rewrites.
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return "true" if key else "false"
    if key is None:
        return "null"
    if isinstance(key, float):
        if math.isnan(key):
            return "NaN"
        if math.isinf(key):
            return "Infinity" if key > 0 else "-Infinity"
        return repr(key)
    if isinstance(key, int):
        return repr(key)
    raise UnserializableArgumentError(
        f"Dictionary keys must be str, int, float, bool or None, "
        f"not '{type(key).__name__}'."
    )


def _sorted_canonical(values: Any, recurse: _Recurse) -> list:
    # set/frozenset have no inherent order, so canonicalize the members first and
    # then order them. Members are often unorderable among themselves (dicts from
    # dataclasses, mixed types), so fall back to ordering by their encoded form.
    members: list[Any] = [recurse(v) for v in values]
    try:
        return sorted(members)
    except TypeError:
        return sorted(members, key=encode_canonical)


def to_canonical(
    obj: Any,
    policy: OpaquePolicy = _DEFAULT_OPAQUE_POLICY,
    recurse: _Recurse | None = None,
) -> CanonicalValue:
    """Normalize one value into the JSON data model.

    ``recurse`` handles nested values; it defaults to this function and exists so a
    subclass canonicalizer stays in control of the whole walk (see
    :class:`~glyff.serialization.JsonArgsCanonicalizer`).

    The mapping is deliberately one-way: bytes become hex, sets become sorted lists,
    and a dataclass contributes only the fields it compares by. What is dropped is
    what identity never depended on.
    """
    if recurse is None:
        recurse = functools.partial(to_canonical, policy=policy)

    if obj is None or isinstance(obj, (str, int, float)):
        # bool is an int subclass, so it lands here too.
        return obj
    if isinstance(obj, type):
        return _qualified_name(obj)
    if isinstance(obj, functools.partial):
        # partials are callable but have no __qualname__; identify them by components.
        return {
            "__partial__": recurse(obj.func),
            "args": [recurse(a) for a in obj.args],
            "keywords": {
                _canonical_key(k): recurse(v) for k, v in obj.keywords.items()
            },
        }
    if dataclasses.is_dataclass(obj):
        return {f.name: recurse(getattr(obj, f.name)) for f in _hashed_fields(obj)}
    if isinstance(obj, dict):
        return {_canonical_key(k): recurse(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [recurse(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return _sorted_canonical(obj, recurse)
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if callable(obj) and hasattr(obj, "__qualname__"):
        return _qualified_name(obj)
    # Tag the policy's output so an opaque value can never collide with a native
    # representation that happens to match it.
    return {_OPAQUE_TAG: recurse(policy.hash(OpaqueContext(value=obj)))}


def _reject(obj: Any) -> Any:
    raise UnserializableArgumentError(
        f"Value of type '{type(obj).__name__}' is not in the JSON data model, so it "
        "cannot be encoded. Canonicalize it first."
    )


def encode_canonical(value: CanonicalValue) -> bytes:
    """Encode a canonical structure into the bytes that are hashed and stored.

    The single encoder for argument identity: the recorded ``args`` are exactly
    these bytes and ``args_hash`` is exactly their digest, so a migration that
    rewrites arguments recomputes the key by calling this and :func:`args_digest`.
    """
    return stable_json_dumps(value, default=_reject).encode(DEFAULT_ENCODING)


def args_digest(data: bytes) -> str:
    """The execution key's ``args_hash``: a digest over canonical argument bytes."""
    return hashlib.sha256(data).hexdigest()


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
    ensure_ascii: bool = False,
) -> str:
    """Creates a stable JSON string from arbitrary data.

    Non-ASCII characters are emitted as themselves: glyff writes readable JSON
    everywhere, and recorded arguments are read by whoever writes a migration.
    """
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
            "Value could not be serialized to JSON. "
            f"Ensure all components are JSON-serializable. Original error: {e}"
        ) from e


def bind_args(sig: inspect.Signature, args: tuple, kwargs: dict) -> dict[str, Any]:
    """Binds arguments into a name->value dict, including *args/**kwargs.

    Variadic parameters appear as a tuple (var-positional) and dict (var-keyword),
    both of which canonicalize, so they contribute to identity.
    """
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)
