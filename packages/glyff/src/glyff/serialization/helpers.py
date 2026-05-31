import inspect
from typing import Any, Callable

from ..interfaces import IDENTITY_ATTR, Identifiable

_MISSING = object()


def _is_method(func: Callable) -> bool:
    qualname = getattr(func, "__qualname__", "")
    parts = qualname.split(".")
    return len(parts) >= 2 and parts[-2] != "<locals>"


def build_hashable_args(
    func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
) -> dict[str, Any]:
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

        if name in ("self", "cls") and _is_method(func):
            identity_provider = value
            identity = getattr(identity_provider, IDENTITY_ATTR, _MISSING)

            if identity is _MISSING:
                provider_name = (
                    identity_provider.__name__
                    if isinstance(identity_provider, type)
                    else type(identity_provider).__name__
                )
                raise TypeError(
                    f"Method '{func.__qualname__}' is an instance or class method, but "
                    f"'{provider_name}' does not implement the {Identifiable.__name__} protocol "
                    f"(missing attribute '{IDENTITY_ATTR}')."
                )

            args_dict[name] = identity
        else:
            args_dict[name] = value

    return args_dict
