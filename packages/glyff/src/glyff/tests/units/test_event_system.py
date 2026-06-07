from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from glyff.event_system import Event, EventEmitter, EventHandler


@dataclass(frozen=True)
class BaseEvent(Event):
    value: str


@dataclass(frozen=True)
class ChildEvent(BaseEvent):
    pass


@dataclass(frozen=True)
class OtherEvent(Event):
    pass


T = TypeVar("T")


@dataclass(frozen=True)
class GenericEvent(Event, Generic[T]):
    value: T


class BaseEventHandler(EventHandler[BaseEvent]):
    def __init__(self) -> None:
        self.handled: list[Event] = []

    async def handle(self, event: BaseEvent) -> None:
        self.handled.append(event)


class GenericEventHandler(EventHandler[GenericEvent[int]]):
    def __init__(self) -> None:
        self.handled: list[Event] = []

    async def handle(self, event: GenericEvent[int]) -> None:
        self.handled.append(event)


class MultiBaseEventHandler(EventHandler[BaseEvent | Event]):
    def __init__(self) -> None:
        self.handled: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.handled.append(event)


async def test_event_handler_infers_origin_for_generic_event_types():
    handler = GenericEventHandler()
    emitter = EventEmitter([handler])
    event = GenericEvent[int](1)

    await emitter.emit(event)
    await emitter.emit(OtherEvent())

    assert handler.event_types == (GenericEvent,)
    assert handler.handled == [event]


async def test_event_emitter_dispatches_to_base_event_handlers():
    handler = BaseEventHandler()
    emitter = EventEmitter([handler])
    event = ChildEvent("child")

    await emitter.emit(event)

    assert handler.handled == [event]


async def test_event_emitter_deduplicates_handlers_registered_for_multiple_bases():
    handler = MultiBaseEventHandler()
    emitter = EventEmitter([handler])
    event = ChildEvent("child")

    await emitter.emit(event)

    assert handler.event_types == (BaseEvent, Event)
    assert handler.handled == [event]
