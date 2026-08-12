import dataclasses
import functools
import math
from typing import Any, Callable, TypeAlias

from .._execution import (
    CanonicalFallback,
    CanonicalValue,
    encode_canonical,
    make_fallback_marker,
    require_unreserved_canonical_mapping,
)
from ..exceptions import ArgumentCanonicalizationError
from ._fallback import CanonicalFallbackRepresenter

_Recurse: TypeAlias = Callable[[Any], CanonicalValue]
_UNREPRESENTABLE = object()


def _qualified_name(obj: Any) -> str:
    return f"{obj.__module__}.{obj.__qualname__}"


def _identity_fields(obj: Any) -> list[dataclasses.Field]:
    return [
        field
        for field in dataclasses.fields(obj)
        if (field.hash if field.hash is not None else field.compare)
    ]


def _canonical_key(key: Any) -> str:
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


def _canonicalize_set(values: Any, recurse: _Recurse) -> list[CanonicalValue]:
    members = [recurse(value) for value in values]
    try:
        return sorted(members)  # type: ignore[type-var]
    except TypeError:
        return sorted(members, key=encode_canonical)


def require_canonical_value(value: CanonicalValue) -> None:
    """Validate a value supplied through a canonicalization extension point."""
    encode_canonical(value)


def to_canonical(
    obj: Any,
    fallback_representer: CanonicalFallbackRepresenter,
    recurse: _Recurse | None = None,
) -> CanonicalValue:
    """Normalize one value into the JSON data model."""
    if recurse is None:
        recurse = functools.partial(
            to_canonical, fallback_representer=fallback_representer
        )

    if isinstance(obj, CanonicalFallback):
        require_canonical_value(obj.representation)
        return make_fallback_marker(obj.representation)

    derived = _derive_canonical(obj, recurse)
    if derived is _UNREPRESENTABLE:
        representation = fallback_representer.represent(obj)
        require_canonical_value(representation)
        return make_fallback_marker(representation)
    require_unreserved_canonical_mapping(derived)
    return derived


def _derive_canonical(obj: Any, recurse: _Recurse) -> Any:
    if obj is None or isinstance(obj, (str, int, float)):
        return obj
    if isinstance(obj, type):
        return _qualified_name(obj)
    if isinstance(obj, functools.partial):
        return {
            "__partial__": recurse(obj.func),
            "args": [recurse(argument) for argument in obj.args],
            "keywords": _canonical_mapping(obj.keywords, recurse),
        }
    if dataclasses.is_dataclass(obj):
        return {
            field.name: recurse(getattr(obj, field.name))
            for field in _identity_fields(obj)
        }
    if isinstance(obj, dict):
        return _canonical_mapping(obj, recurse)
    if isinstance(obj, (list, tuple)):
        return [recurse(value) for value in obj]
    if isinstance(obj, (set, frozenset)):
        return _canonicalize_set(obj, recurse)
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if callable(obj) and hasattr(obj, "__qualname__"):
        return _qualified_name(obj)
    return _UNREPRESENTABLE
