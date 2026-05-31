from __future__ import annotations

from typing import Any, Callable, TypeVar, overload

from .interfaces import IDENTITY_ATTR

T = TypeVar("T")


@overload
def identify(identity: str) -> Callable[[type[T]], type[T]]: ...


@overload
def identify(identity: str, instance: object) -> None: ...


def identify(identity: str, instance: object | None = None) -> Any:
    """Attach a stable identity to an object for argument hashing.

    The identity is stored in the ``__glyff_identity__`` attribute, satisfying
    the :class:`~glyff.interfaces.Identifiable` protocol.

    As a decorator -- one static identity shared by every instance of a class::

        @identify("my-agent")
        class MyAgent:
            ...

    As a function -- an identity for a single instance::

        agent = MyAgent()
        identify("my-agent", agent)

    The identity is the leading argument in both forms so the two overloads
    differ only by arity, keeping them unambiguous for type checkers.
    """
    if not isinstance(identity, str):
        raise TypeError(
            "identify() takes the identity string first: "
            "use @identify('id') or identify('id', instance)."
        )

    if instance is None:

        def decorator(cls: type[T]) -> type[T]:
            setattr(cls, IDENTITY_ATTR, identity)
            return cls

        return decorator

    setattr(instance, IDENTITY_ATTR, identity)
    return None
