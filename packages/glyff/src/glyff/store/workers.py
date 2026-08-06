"""Running a store's blocking work on a thread without abandoning it.

Backend support, not core API: nothing in glyff's own contracts mentions this.
It is public because the shipped out-of-tree backends use it, so it carries the
same stability promise as the rest of the supported surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

__all__ = ["run_to_completion"]

T = TypeVar("T")


async def run_to_completion(work: Callable[[], T]) -> T:
    """Runs ``work`` on a worker thread, waiting for it however many times the
    caller is cancelled.

    Cancelling does not stop the thread. A caller holding a store exclusively
    must not leave before its worker is done: the next writer would take the
    lock and act on state the abandoned worker is still replacing. So
    cancellation is absorbed until the work has settled, then re-raised.
    """
    worker = asyncio.ensure_future(asyncio.to_thread(work))
    cancellation: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.wait([worker])
        except asyncio.CancelledError as cancelled:
            cancellation = cancelled

    if cancellation is not None:
        # Collected so work that also failed is not reported as an exception
        # nobody retrieved. The cancellation is what the caller asked for.
        worker.exception()
        raise cancellation
    return worker.result()
