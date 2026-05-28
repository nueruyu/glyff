import inspect
from typing import Any, Callable


def build_hashable_args(
    func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
) -> dict[str, Any]:
    """
    Binds arguments to a signature and builds a dictionary of hashable arguments,
    including a special '__glyff_identity__' for methods.
    """
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

        if name in ("self", "cls"):
            identity_provider = value
            identity_method = getattr(identity_provider, "__glyff_identity__", None)

            if not callable(identity_method):
                raise TypeError(
                    f"Method '{func.__qualname__}' is an instance or class method, but "
                    f"the '{type(identity_provider).__name__}' instance/class does not "
                    "implement a callable '__glyff_identity__' method."
                )

            args_dict["__glyff_identity__"] = identity_method()
        else:
            args_dict[name] = value

    return args_dict
