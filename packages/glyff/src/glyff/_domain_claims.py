from __future__ import annotations

import asyncio

from ._types import DomainId, DomainVersion, SessionId
from ._interfaces import Backend
from .exceptions import DomainVersionMismatchError


class DomainClaims:
    """The domain versions this session has observed in the store.

    Claiming is all this does: a version it disagrees with is reported, never
    migrated.
    """

    def __init__(self, *, backend: Backend, session_id: SessionId) -> None:
        self._backend = backend
        self._session_id = session_id
        # What the store said, not what was agreed: a version read while
        # refusing a mismatch still spares the round trip for a process that
        # goes on to carry the right one.
        self._observed: dict[DomainId, DomainVersion] = {}
        self._locks: dict[DomainId, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    async def ensure(self, domain_id: DomainId, version: DomainVersion) -> None:
        """Claims or verifies ``version`` for ``domain_id`` in this session.

        Raises :class:`DomainVersionMismatchError` if the session's records
        belong to another version, leaving them untouched.
        """
        observed = self._observed.get(domain_id)
        if observed is None:
            observed = await self._observe(domain_id, version)
        self._require(domain_id, version, observed)

    async def _observe(
        self, domain_id: DomainId, version: DomainVersion
    ) -> DomainVersion:
        async with self._meta_lock:
            lock = self._locks.setdefault(domain_id, asyncio.Lock())

        async with lock:
            observed = self._observed.get(domain_id)
            if observed is None:
                # Only a claim that came back is recorded: one that failed or
                # was cancelled observed nothing, and the claim is atomic and
                # idempotent, so asking again is free.
                observed = await self._backend.claim_domain(
                    self._session_id, domain_id, version
                )
                self._observed[domain_id] = observed
            return observed

    def _require(
        self, domain_id: DomainId, version: DomainVersion, recorded: DomainVersion
    ) -> None:
        if recorded != version:
            raise DomainVersionMismatchError(
                f"Session {self._session_id} recorded domain {domain_id} at "
                f"version {recorded.value!r}, but this process runs "
                f"{version.value!r}. Take "
                "the session offline and migrate it, pin it to the code that "
                "started it, or start a new one.",
                domain_id=domain_id,
                recorded_version=recorded,
                current_version=version,
            )
