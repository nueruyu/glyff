"""Entering a domain's function claims or verifies its version — and no more."""

import asyncio

import pytest
from glyff import (
    ArgumentCanonicalizer,
    Domain,
    DomainId,
    DomainVersion,
    Serializer,
    Session,
    SessionId,
)
from glyff.exceptions import DomainVersionMismatchError
from glyff.store import MemoryBackend

SESSION = SessionId("session")
PAYMENTS = DomainId("com.example.payments")
SHIPPING = DomainId("com.example.shipping")


def domain(id: DomainId, version: str) -> Domain:
    return Domain(id, version=DomainVersion(version))


def test_a_domain_is_not_hashable():
    payment_domain = domain(PAYMENTS, "v1")

    with pytest.raises(TypeError):
        hash(payment_domain)


async def test_an_engraved_function_captures_the_domain_values_at_decoration(
    serializer, argument_canonicalizer
):
    payment_domain = domain(PAYMENTS, "v1")
    task = _task(payment_domain)
    payment_domain._version = DomainVersion("v2")

    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer):
        await task()

    assert await backend.claim_domain(
        SESSION, PAYMENTS, DomainVersion("other")
    ) == DomainVersion("v1")


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
            calls.append(domain.version.value)
        return domain.version.value

    return task


async def test_a_first_call_records_the_domains_version(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()
    task = _task(domain(PAYMENTS, "v1"))

    async with _session(backend, serializer, argument_canonicalizer):
        await task()

    assert await backend.claim_domain(
        SESSION, PAYMENTS, DomainVersion("v2")
    ) == DomainVersion("v1")


async def test_entering_under_the_recorded_version_is_accepted(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()
    task = _task(domain(PAYMENTS, "v1"))

    for _ in range(2):
        async with _session(backend, serializer, argument_canonicalizer):
            assert await task() == "v1"


async def test_a_different_version_is_refused(serializer, argument_canonicalizer):
    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer):
        await _task(domain(PAYMENTS, "v1"))()

    async with _session(backend, serializer, argument_canonicalizer):
        with pytest.raises(DomainVersionMismatchError) as raised:
            await _task(domain(PAYMENTS, "v2"))()

    assert raised.value.domain_id == PAYMENTS
    assert raised.value.recorded_version == DomainVersion("v1")
    assert raised.value.current_version == DomainVersion("v2")


async def test_a_mismatch_leaves_the_session_alone(serializer, argument_canonicalizer):
    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer):
        await _task(domain(PAYMENTS, "v1"))()

    async with _session(backend, serializer, argument_canonicalizer):
        with pytest.raises(DomainVersionMismatchError):
            await _task(domain(PAYMENTS, "v2"))()

    assert await backend.claim_domain(
        SESSION, PAYMENTS, DomainVersion("v-later")
    ) == DomainVersion("v1")
    async with _session(backend, serializer, argument_canonicalizer):
        assert [
            e.id.name.value async for e in backend.repository.executions(SESSION)
        ] == ["_task.<locals>.task"]


async def test_a_mismatch_is_raised_on_entering_the_domain_not_the_session(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer):
        await _task(domain(PAYMENTS, "v1"))()

    shipping = _task(domain(SHIPPING, "v1"))
    payments = _task(domain(PAYMENTS, "v2"))

    async with _session(backend, serializer, argument_canonicalizer):
        await shipping()
        with pytest.raises(DomainVersionMismatchError):
            await payments()


async def test_domains_in_one_session_carry_their_own_versions(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()

    async with _session(backend, serializer, argument_canonicalizer):
        await _task(domain(PAYMENTS, "v1"))()
        await _task(domain(SHIPPING, "v2"))()

    assert await backend.claim_domain(
        SESSION, PAYMENTS, DomainVersion("other")
    ) == DomainVersion("v1")
    assert await backend.claim_domain(
        SESSION, SHIPPING, DomainVersion("other")
    ) == DomainVersion("v2")


async def test_sessions_in_one_backend_carry_their_own_versions(
    serializer, argument_canonicalizer
):
    backend = MemoryBackend()
    orders, refunds = SessionId("orders"), SessionId("refunds")

    async with _session(backend, serializer, argument_canonicalizer, orders):
        await _task(domain(PAYMENTS, "v1"))()
    async with _session(backend, serializer, argument_canonicalizer, refunds):
        await _task(domain(PAYMENTS, "v2"))()

    assert await backend.claim_domain(
        orders, PAYMENTS, DomainVersion("other")
    ) == DomainVersion("v1")
    assert await backend.claim_domain(
        refunds, PAYMENTS, DomainVersion("other")
    ) == DomainVersion("v2")


async def test_concurrent_first_calls_claim_once(serializer, argument_canonicalizer):
    backend = MemoryBackend()
    payment_domain = domain(PAYMENTS, "v1")
    tasks = [_task(payment_domain) for _ in range(8)]
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
    backend = MemoryBackend()
    async with _session(backend, serializer, argument_canonicalizer):
        await _task(domain(PAYMENTS, "v1"))()

    claims = 0
    claim_domain = backend.claim_domain

    async def counting(*args, **kwargs):
        nonlocal claims
        claims += 1
        return await claim_domain(*args, **kwargs)

    backend.claim_domain = counting  # type: ignore[method-assign]

    async with _session(backend, serializer, argument_canonicalizer):
        with pytest.raises(DomainVersionMismatchError):
            await _task(domain(PAYMENTS, "v2"))()
        assert await _task(domain(PAYMENTS, "v1"))() == "v1"

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
