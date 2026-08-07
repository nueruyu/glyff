"""Entering a domain's function claims or verifies its version — and no more."""

import asyncio

import pytest
from glyff import (
    ArgumentCanonicalizer,
    Domain,
    DomainId,
    Serializer,
    Session,
    SessionId,
)
from glyff.exceptions import DomainVersionMismatchError
from glyff.store import MemoryBackend

SESSION = SessionId("session")
PAYMENTS = DomainId("com.example.payments")
SHIPPING = DomainId("com.example.shipping")


def _session(
    backend: MemoryBackend,
    serializer: Serializer,
    argument_canonicalizer: ArgumentCanonicalizer,
    session_id: SessionId = SESSION,
) -> Session:
    return Session(
        id=session_id,
        backend=backend,
        serializer=serializer,
        argument_canonicalizer=argument_canonicalizer,
    )


def _task(domain: Domain, calls: list[str] | None = None):
    @domain.engrave
    async def task() -> str:
        if calls is not None:
            calls.append(domain.version)
        return domain.version

    return task


async def test_a_first_call_records_the_domains_version(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()
    task = _task(Domain(PAYMENTS, version="v1"))

    async with _session(backend, serializer, argument_canonicalizer):
        await task()

    assert await backend.claim_domain(SESSION, PAYMENTS, "v2") == "v1"


async def test_entering_under_the_recorded_version_is_accepted(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()
    task = _task(Domain(PAYMENTS, version="v1"))

    for _ in range(2):
        async with _session(backend, serializer, argument_canonicalizer):
            assert await task() == "v1"


async def test_a_different_version_is_refused(serializer, argument_canonicalizer):
    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer):
        await _task(Domain(PAYMENTS, version="v1"))()

    async with _session(backend, serializer, argument_canonicalizer):
        with pytest.raises(DomainVersionMismatchError) as raised:
            await _task(Domain(PAYMENTS, version="v2"))()

    assert raised.value.domain_id == PAYMENTS
    assert raised.value.recorded_version == "v1"
    assert raised.value.current_version == "v2"


async def test_a_mismatch_leaves_the_session_alone(serializer, argument_canonicalizer):
    # The refusal is a claim that disagreed, not the first step of a migration.
    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer):
        await _task(Domain(PAYMENTS, version="v1"))()

    async with _session(backend, serializer, argument_canonicalizer):
        with pytest.raises(DomainVersionMismatchError):
            await _task(Domain(PAYMENTS, version="v2"))()

    assert await backend.claim_domain(SESSION, PAYMENTS, "v-later") == "v1"
    async with _session(backend, serializer, argument_canonicalizer):
        assert [
            e.id.name.value async for e in backend.repository.executions(SESSION)
        ] == ["_task.<locals>.task"]


async def test_a_mismatch_is_raised_on_entering_the_domain_not_the_session(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer):
        await _task(Domain(PAYMENTS, version="v1"))()

    shipping = _task(Domain(SHIPPING, version="v1"))
    payments = _task(Domain(PAYMENTS, version="v2"))

    # Entering the session is not where this fails: another domain runs first.
    async with _session(backend, serializer, argument_canonicalizer):
        await shipping()
        with pytest.raises(DomainVersionMismatchError):
            await payments()


async def test_domains_in_one_session_carry_their_own_versions(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()

    async with _session(backend, serializer, argument_canonicalizer):
        await _task(Domain(PAYMENTS, version="v1"))()
        await _task(Domain(SHIPPING, version="v2"))()

    assert await backend.claim_domain(SESSION, PAYMENTS, "other") == "v1"
    assert await backend.claim_domain(SESSION, SHIPPING, "other") == "v2"


async def test_sessions_in_one_backend_carry_their_own_versions(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()
    orders, refunds = SessionId("orders"), SessionId("refunds")

    async with _session(backend, serializer, argument_canonicalizer, orders):
        await _task(Domain(PAYMENTS, version="v1"))()
    async with _session(backend, serializer, argument_canonicalizer, refunds):
        await _task(Domain(PAYMENTS, version="v2"))()

    assert await backend.claim_domain(orders, PAYMENTS, "other") == "v1"
    assert await backend.claim_domain(refunds, PAYMENTS, "other") == "v2"


async def test_concurrent_first_calls_claim_once(serializer, argument_canonicalizer):
    backend = MemoryBackend()
    domain = Domain(PAYMENTS, version="v1")
    tasks = [_task(domain) for _ in range(8)]
    claims = 0
    claim_domain = backend.claim_domain

    async def counting(*args, **kwargs):
        nonlocal claims
        claims += 1
        return await claim_domain(*args, **kwargs)

    backend.claim_domain = counting  # type: ignore[method-assign]

    async with _session(backend, serializer, argument_canonicalizer):
        await asyncio.gather(*(task() for task in tasks))

    assert claims == 1


async def test_a_mismatch_is_observed_without_a_second_round_trip(
    serializer, argument_canonicalizer
):
    # The version a refused claim reported is worth keeping: a process that goes
    # on to carry it should not have to ask again.
    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer):
        await _task(Domain(PAYMENTS, version="v1"))()

    claims = 0
    claim_domain = backend.claim_domain

    async def counting(*args, **kwargs):
        nonlocal claims
        claims += 1
        return await claim_domain(*args, **kwargs)

    backend.claim_domain = counting  # type: ignore[method-assign]

    async with _session(backend, serializer, argument_canonicalizer):
        with pytest.raises(DomainVersionMismatchError):
            await _task(Domain(PAYMENTS, version="v2"))()
        assert await _task(Domain(PAYMENTS, version="v1"))() == "v1"

    assert claims == 1


def test_an_empty_session_id_is_refused():
    with pytest.raises(ValueError):
        SessionId("")


@pytest.mark.parametrize(
    "value", [".", "..", ".hidden", "a/b", "a\\b", "a:b", " padded ", "%2E"]
)
def test_a_path_shaped_session_id_is_still_a_name(value: str):
    # Stores encode the name into whatever their keys allow, so core does not
    # narrow what an application may call its sessions.
    assert SessionId(value).value == value
