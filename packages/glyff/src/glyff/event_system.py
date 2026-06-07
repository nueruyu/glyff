from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from types import UnionType
from typing import Generic, TypeVar, Union, get_args, get_origin


class Event(ABC):
    """
    Base class for all events emitted during a session.
    """

    pass


E = TypeVar("E", bound=Event)


def _event_types_from_type_arg(type_arg: object) -> tuple[type[Event], ...]:
    origin = get_origin(type_arg)
    if origin in (Union, UnionType):
        return tuple(
            event_type
            for arg in get_args(type_arg)
            for event_type in _event_types_from_type_arg(arg)
        )
    if isinstance(origin, type) and issubclass(origin, Event):
        return (origin,)
    if origin is None and isinstance(type_arg, type) and issubclass(type_arg, Event):
        return (type_arg,)
    return ()


class EventHandler(ABC, Generic[E]):
    """
    An interface for handling events emitted during a session.
    """

    @property
    def event_types(self) -> tuple[type[Event], ...]:
        """Returns the event types this handler can process."""
        return self._infer_event_types()

    @classmethod
    def _infer_event_types(cls) -> tuple[type[Event], ...]:
        for handler_cls in cls.__mro__:
            for base in getattr(handler_cls, "__orig_bases__", ()):
                if get_origin(base) is EventHandler:
                    args = get_args(base)
                    if not args:
                        continue
                    event_types = _event_types_from_type_arg(args[0])
                    if event_types:
                        return event_types
        return (Event,)

    @abstractmethod
    async def handle(self, event: E) -> None:
        """Handles an emitted event."""
        ...


class EventEmitter:
    """Manages event handlers and dispatches events."""

    def __init__(self, handlers: list[EventHandler]) -> None:
        self._handler_map = self._resolve_handler_map(handlers)

    def _resolve_handler_map(
        self, handlers: list[EventHandler]
    ) -> dict[type[Event], list[EventHandler]]:
        """Inspects handlers to map event types to the handlers that process them."""
        handler_map: dict[type[Event], list[EventHandler]] = defaultdict(list)
        for handler in handlers:
            for event_type in handler.event_types:
                handler_map[event_type].append(handler)
        return handler_map

    async def emit(self, event: Event) -> None:
        """Dispatches an event to all handlers registered for its type."""
        handlers_to_run: list[EventHandler] = []
        seen_handlers: set[EventHandler] = set()
        for event_type in type(event).__mro__:
            if not issubclass(event_type, Event):
                continue
            for handler in self._handler_map.get(event_type, []):
                if handler in seen_handlers:
                    continue
                seen_handlers.add(handler)
                handlers_to_run.append(handler)
        for handler in handlers_to_run:
            await handler.handle(event)
