import inspect
from typing import Any, Callable


def _require_identity(func: Callable, provider: Any) -> Any:
    identity_method = getattr(provider, "__glyff_identity__", None)
    if not callable(identity_method):
        raise TypeError(
            f"Method '{func.__qualname__}' is an instance or class method, but "
            f"the '{type(provider).__name__}' instance/class does not "
            "implement a callable '__glyff_identity__' method."
        )
    # When provider is a class (classmethod case), __glyff_identity__ is retrieved
    # as an unbound function — pass the class as self explicitly.
    if inspect.ismethod(identity_method):
        return identity_method()
    return identity_method(provider)


def build_hashable_args(
    func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
) -> dict[str, Any]:
    """
    Binds arguments to a signature and builds a dictionary of hashable arguments,
    including a special '__glyff_identity__' for methods.
    """
    args_dict: dict[str, Any] = {}

    # When func is a bound method (instance method or classmethod), inspect.signature
    # strips self/cls from the signature. Callers may still prepend self/cls to args,
    # so detect and extract it before binding.
    actual_args = args
    if inspect.ismethod(func) and args and args[0] is func.__self__:
        args_dict["__glyff_identity__"] = _require_identity(func, args[0])
        actual_args = args[1:]

    bound = sig.bind(*actual_args, **kwargs)
    bound.apply_defaults()

    for name, value in bound.arguments.items():
        param = sig.parameters[name]
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        if name in ("self", "cls"):
            # Unbound method: self/cls is still present in the signature.
            args_dict["__glyff_identity__"] = _require_identity(func, value)
        else:
            args_dict[name] = value

    return args_dict
