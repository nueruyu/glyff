from __future__ import annotations

from typing import Any, Callable, Type, overload

from .interfaces import IDENTITY_ATTR


@overload
def identify(identity: str) -> Callable[[Type[Any]], Type[Any]]: ...


@overload
def identify(instance: object, identity: str) -> None: ...


def identify(arg1: str | object, arg2: str | None = None) -> Any:
    """Attach a stable identity to an object for argument hashing.

    The identity is stored in the ``__glyff_identity__`` attribute, satisfying
    the :class:`~glyff.interfaces.Identifiable` protocol.

    As a decorator (one static identity shared by every instance of a class)::

        @identify("my-static-id")
        class MyAgent:
            ...

    As a function (an identity for a single instance)::

        agent = MyAgent()
        identify(agent, "instance-specific-id")
    """
    # Decorator usage: @identify("some-id")
    if isinstance(arg1, str) and arg2 is None:
        identity_value = arg1

        def decorator(cls: Type[Any]) -> Type[Any]:
            setattr(cls, IDENTITY_ATTR, identity_value)
            return cls

        return decorator

    # Function usage: identify(instance, "some-id")
    if not isinstance(arg1, str) and isinstance(arg2, str):
        setattr(arg1, IDENTITY_ATTR, arg2)
        return None

    raise TypeError(
        "Invalid arguments for identify(). Use it as a decorator -- "
        "@identify('id') -- or as a function -- identify(instance, 'id')."
    )
