import inspect
from typing import Any, Callable


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
            identity_method = getattr(identity_provider, "__glyff_identity__", None)

            if not callable(identity_method):
                provider_name = (
                    identity_provider.__name__
                    if isinstance(identity_provider, type)
                    else type(identity_provider).__name__
                )
                raise TypeError(
                    f"Method '{func.__qualname__}' is an instance or class method, but "
                    f"the '{provider_name}' instance/class does not "
                    "implement a callable '__glyff_identity__' method."
                )

            args_dict[name] = identity_method()
        else:
            args_dict[name] = value

    return args_dict
