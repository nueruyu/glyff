"""Canonical argument encoding and JSON serialization helpers."""

import dataclasses
import functools
import json
import math
from abc import ABC, abstractmethod
from typing import Any, Callable, TypeAlias

from .._execution import CanonicalValue
from ..exceptions import ArgumentCanonicalizationError
from .constants import DEFAULT_ENCODING, JSON_SEPARATORS

# A key reserved for glyff, so a policy's output can never be mistaken for a
# native representation that happens to match it. `_canonical_mapping` refuses a
# mapping that claims it, which is what makes the namespace real rather than
# merely unlikely.
_OPAQUE_TAG = "__glyff_opaque__"


@dataclasses.dataclass(frozen=True)
class Opaque:
    """A value already standing in for one that has no value representation.

    What an `OpaquePolicy` returned, carried as itself. Canonicalizing one
    yields the marker again, which is how a recorded argument goes back through
    a canonicalizer without a mapping being able to pass itself off as one.
    """

    value: CanonicalValue


def as_opaque(representation: CanonicalValue) -> CanonicalValue:
    """The canonical form of a value an `OpaquePolicy` stood in for."""
    return {_OPAQUE_TAG: representation}


def is_opaque(value: CanonicalValue) -> bool:
    """Whether ``value`` is what :func:`as_opaque` writes."""
    return isinstance(value, dict) and len(value) == 1 and _OPAQUE_TAG in value


def opaque_representation(value: CanonicalValue) -> CanonicalValue:
    """What the policy returned for a value :func:`is_opaque` accepts."""
    assert isinstance(value, dict)
    return value[_OPAQUE_TAG]


def _qualified_name(obj: Any) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


class OpaquePolicy(ABC):
    """Maps a value with no value representation to a canonical one."""

    @abstractmethod
    def represent(self, value: Any) -> Any:
        """Return a JSON-encodable representation of ``value``, or raise to reject it."""
        ...


class RejectOpaque(OpaquePolicy):
    """Default policy: reject opaque values so distinct instances never silently collide."""

    def represent(self, value: Any) -> Any:
        raise ArgumentCanonicalizationError(
            f"Cannot canonicalize opaque value of type '{type(value).__name__}': it "
            "has no value representation. Give it a serializable representation "
            "(e.g. a dataclass or model), or pass an opaque policy to the "
            "canonicalizer."
        )


class OpaqueByTypeQualname(OpaquePolicy):
    """Opt-in policy: identify an opaque value by its class' qualified name.

    Collapses every instance of a class to one representation. Safe only when the
    value is stateless with respect to the result (e.g. a service holding injected
    dependencies), since distinct instances then canonicalize identically.
    """

    def represent(self, value: Any) -> Any:
        return _qualified_name(type(value))


# Shared, stateless singleton used as the default wherever a policy is optional.
_DEFAULT_OPAQUE_POLICY: OpaquePolicy = RejectOpaque()

_Recurse: TypeAlias = Callable[[Any], "CanonicalValue"]


def _identity_fields(obj: Any) -> list[dataclasses.Field]:
    # A field the dataclass excludes from equality never distinguished two calls.
    return [
        f
        for f in dataclasses.fields(obj)
        if (f.hash if f.hash is not None else f.compare)
    ]


def _canonical_key(key: Any) -> str:
    # Stringify keys here rather than at encoding time, so the form stays stable
    # across a JSON round trip. Always through the builtin, as json's own encoder
    # does: a subclass' __str__ or __repr__ must not reach identity.
    if isinstance(key, str):
        return str.__str__(key)
    if isinstance(key, bool):
        return "true" if key else "false"
    if key is None:
        return "null"
    if isinstance(key, float):
        if math.isnan(key):
            return "NaN"
        if math.isinf(key):
            return "Infinity" if key > 0 else "-Infinity"
        return float.__repr__(key)
    if isinstance(key, int):
        return int.__repr__(key)
    raise ArgumentCanonicalizationError(
        f"Dictionary keys must be str, int, float, bool or None, "
        f"not '{type(key).__name__}'."
    )


def _canonical_mapping(obj: dict, recurse: _Recurse) -> dict[str, CanonicalValue]:
    canonical: dict[str, CanonicalValue] = {}
    for key, value in obj.items():
        name = _canonical_key(key)
        if name in canonical:
            raise ArgumentCanonicalizationError(
                f"Two keys of this mapping canonicalize to {name!r}. Distinct "
                "arguments must stay distinct, or the calls would collapse onto "
                "one execution key."
            )
        canonical[name] = recurse(value)
    return canonical


def _canonicalize_set(values: Any, recurse: _Recurse) -> list:
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
    :class:`~glyff.serialization.JsonArgumentCanonicalizer`).

    The mapping is deliberately one-way: what it drops is what identity never
    depended on.
    """
    if recurse is None:
        recurse = functools.partial(to_canonical, policy=policy)

    if isinstance(obj, Opaque):
        # Already the policy's answer, so it is kept rather than asked again.
        return as_opaque(recurse(obj.value))

    derived = _derive(obj, policy, recurse)
    if derived is _UNREPRESENTABLE:
        return as_opaque(recurse(policy.represent(obj)))
    if isinstance(derived, dict) and _OPAQUE_TAG in derived:
        # Every mapping glyff derives from a value passes here, whichever branch
        # built it, so nothing can claim the tag by another route.
        raise ArgumentCanonicalizationError(
            f"{_OPAQUE_TAG!r} is reserved: it is how glyff records a value an "
            "opaque policy stood in for, and a value canonicalizing to it would "
            "collide with one. Name the key or field something else."
        )
    return derived


_UNREPRESENTABLE = object()


def _derive(obj: Any, policy: OpaquePolicy, recurse: _Recurse) -> Any:
    """One value's canonical form, or ``_UNREPRESENTABLE`` if it has none."""
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
            "keywords": _canonical_mapping(obj.keywords, recurse),
        }
    if dataclasses.is_dataclass(obj):
        return {f.name: recurse(getattr(obj, f.name)) for f in _identity_fields(obj)}
    if isinstance(obj, dict):
        return _canonical_mapping(obj, recurse)
    if isinstance(obj, (list, tuple)):
        return [recurse(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return _canonicalize_set(obj, recurse)
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if callable(obj) and hasattr(obj, "__qualname__"):
        return _qualified_name(obj)
    return _UNREPRESENTABLE


def _reject(obj: Any) -> Any:
    raise ArgumentCanonicalizationError(
        f"Value of type '{type(obj).__name__}' is not in the JSON data model, so it "
        "cannot be encoded. Canonicalize it first."
    )


def encode_canonical(value: CanonicalValue) -> bytes:
    """The single encoder for argument identity. See :attr:`~glyff.Execution.arguments`."""
    return stable_json_dumps(value, default=_reject).encode(DEFAULT_ENCODING)


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

    Raises ``TypeError`` for anything ``default`` cannot handle; each caller wraps
    that in the error its own boundary means.
    """
    return json.dumps(
        data,
        indent=indent,
        sort_keys=True,
        ensure_ascii=ensure_ascii,
        default=default or to_serializable,
        separators=JSON_SEPARATORS if indent is None else None,
    )
