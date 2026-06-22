import asyncio
from collections import defaultdict

from ._models import ExecutionId

_SequenceKey = tuple[ExecutionId | None, str, str]


class Sequencer:
    """
    Generates sequential integers for ExecutionIds in a concurrency-safe manner.
    Each (parent_id, name, args_hash) tuple has its own independent sequence.
    """

    def __init__(self):
        self._locks: dict[_SequenceKey, asyncio.Lock] = {}
        self._counters: dict[_SequenceKey, int] = defaultdict(int)
        self._meta_lock = asyncio.Lock()

    async def next(
        self, parent: ExecutionId | None, name: str, args_hash: str
    ) -> int:
        """Returns the next sequence number for the given content scope."""
        key = (parent, name, args_hash)

        async with self._meta_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()

        async with self._locks[key]:
            seq = self._counters[key]
            self._counters[key] += 1
            return seq

    async def reset_for_call(self, execution_id: ExecutionId) -> None:
        """
        Resets all counters that are children of the given ExecutionId.
        This is crucial for deterministic re-execution of a parent task.
        """
        async with self._meta_lock:
            keys_to_reset = [key for key in self._counters if key[0] == execution_id]
            for key in keys_to_reset:
                del self._counters[key]
                del self._locks[key]
