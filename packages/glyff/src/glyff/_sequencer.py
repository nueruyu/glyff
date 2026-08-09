import asyncio
from collections import defaultdict

from ._identity import (
    ArgumentsDigest,
    DomainId,
    ExecutionId,
    ExecutionName,
    SequenceScope,
)


class Sequencer:
    """
    Generates sequential integers for ExecutionIds in a concurrency-safe manner.
    Each `SequenceScope` has its own independent sequence.
    """

    def __init__(self):
        self._locks: dict[SequenceScope, asyncio.Lock] = {}
        self._counters: dict[SequenceScope, int] = defaultdict(int)
        self._meta_lock = asyncio.Lock()

    async def next(
        self,
        parent: ExecutionId | None,
        domain: DomainId,
        name: ExecutionName,
        arguments_digest: ArgumentsDigest,
    ) -> int:
        """Returns the next sequence number for the given content scope."""
        key = SequenceScope(parent, domain, name, arguments_digest)

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
            keys_to_reset = [
                key for key in self._counters if key.parent_id == execution_id
            ]
            for key in keys_to_reset:
                del self._counters[key]
                del self._locks[key]
