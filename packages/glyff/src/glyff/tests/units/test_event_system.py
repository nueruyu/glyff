from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Generic, TypeVar

import pytest

from glyff import Event, EventEmitter, EventHandler


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


async def test_emit_logs_and_continues_when_handler_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    class Boom(EventHandler[Event]):
        async def handle(self, event: Event) -> None:
            calls.append("boom")
            raise RuntimeError("boom")

    class Next(EventHandler[Event]):
        async def handle(self, event: Event) -> None:
            calls.append("next")

    emitter = EventEmitter([Boom(), Next()])

    with caplog.at_level(logging.ERROR, logger="glyff._event_system"):
        await emitter.emit(OtherEvent())

    assert calls == ["boom", "next"]
    assert "Event handler failed: handler=" in caplog.text
    assert "Boom event=OtherEvent" in caplog.text
    assert "RuntimeError: boom" in caplog.text


async def test_emit_does_not_raise_when_handler_raises():
    class Boom(EventHandler[Event]):
        async def handle(self, event: Event) -> None:
            raise RuntimeError("boom")

    emitter = EventEmitter([Boom()])

    await emitter.emit(OtherEvent())
