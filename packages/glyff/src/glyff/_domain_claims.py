from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ._interfaces import Backend
from ._models import DomainId, SessionId
from .exceptions import DomainVersionMismatchError

if TYPE_CHECKING:
    from ._domain import Domain


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
        self._observed: dict[DomainId, str] = {}
        self._locks: dict[DomainId, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    async def ensure(self, domain: Domain) -> None:
        """Claims or verifies ``domain``'s version for this session.

        Raises :class:`DomainVersionMismatchError` if the session's records
        belong to another version, leaving them untouched.
        """
        observed = self._observed.get(domain.id)
        if observed is None:
            observed = await self._observe(domain)
        self._require(domain, observed)

    async def _observe(self, domain: Domain) -> str:
        async with self._meta_lock:
            lock = self._locks.setdefault(domain.id, asyncio.Lock())

        async with lock:
            observed = self._observed.get(domain.id)
            if observed is None:
                # Only a claim that came back is recorded: one that failed or
                # was cancelled observed nothing, and the claim is atomic and
                # idempotent, so asking again is free.
                observed = await self._backend.claim_domain(
                    self._session_id, domain.id, domain.version
                )
                self._observed[domain.id] = observed
            return observed

    def _require(self, domain: Domain, recorded: str) -> None:
        if recorded != domain.version:
            raise DomainVersionMismatchError(
                f"Session {self._session_id} recorded domain {domain.id} at "
                f"version {recorded!r}, but this process runs "
                f"{domain.version!r}. Take the session offline and migrate it, "
                "pin it to the code that started it, or start a new one.",
                domain_id=domain.id,
                recorded_version=recorded,
                current_version=domain.version,
            )
