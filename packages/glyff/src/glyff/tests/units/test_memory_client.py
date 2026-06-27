"""Unit tests for ``MemoryClient`` read-your-writes consistency: reads within
an uncommitted transaction must observe the staged view, consistent with
``all_keys()``."""

import pytest

from glyff.store import MemoryClient
from glyff.store._memory import _MemoryTransaction


async def _commit_write(client: MemoryClient, key: str, value: bytes) -> None:
    token, _ = client.begin_staging()
    try:
        client.stage_write(key, value)
        await client.commit_staged()
    finally:
        client.end_staging(token)


async def test_read_observes_staged_write():
    client = MemoryClient()
    token, _ = client.begin_staging()
    try:
        client.stage_write("k", b"v")
        assert await client.read("k") == b"v"
    finally:
        client.end_staging(token)


async def test_staged_write_overrides_committed_value():
    client = MemoryClient()
    await _commit_write(client, "k", b"old")

    token, _ = client.begin_staging()
    try:
        client.stage_write("k", b"new")
        assert await client.read("k") == b"new"
        assert client.data["k"] == b"old"
    finally:
        client.end_staging(token)


async def test_read_observes_staged_delete():
    client = MemoryClient()
    await _commit_write(client, "k", b"v")

    token, _ = client.begin_staging()
    try:
        client.stage_delete("k")
        assert await client.read("k") is None
    finally:
        client.end_staging(token)


async def test_read_falls_through_to_committed_value():
    client = MemoryClient()
    await _commit_write(client, "k", b"v")
    assert await client.read("k") == b"v"


async def test_clear_staged_discards_staged_view():
    client = MemoryClient()
    token, _ = client.begin_staging()
    try:
        client.stage_write("k", b"v")
        client.clear_staged()
        assert await client.read("k") is None
    finally:
        client.end_staging(token)


async def test_read_consistent_with_all_keys():
    client = MemoryClient()
    await _commit_write(client, "committed", b"c")

    token, _ = client.begin_staging()
    try:
        client.stage_write("staged", b"s")
        client.stage_delete("committed")

        assert client.all_keys() == {"staged"}
        assert await client.read("staged") == b"s"
        assert await client.read("committed") is None
    finally:
        client.end_staging(token)


async def test_read_staged_false_returns_committed_ignoring_staged():
    client = MemoryClient()
    await _commit_write(client, "k", b"committed")

    token, _ = client.begin_staging()
    try:
        client.stage_write("k", b"staged")
        assert await client.read("k") == b"staged"
        assert await client.read("k", staged=False) == b"committed"

        client.stage_delete("k")
        assert await client.read("k") is None
        assert await client.read("k", staged=False) == b"committed"
    finally:
        client.end_staging(token)


async def test_write_operations_require_staging():
    client = MemoryClient()

    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_write("k", b"v")
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_delete("k")
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.clear_staged()
    with pytest.raises(RuntimeError, match="outside a transaction"):
        await client.commit_staged()


async def test_all_keys_without_staging_returns_committed_keys():
    client = MemoryClient()
    await _commit_write(client, "k", b"v")

    assert client.all_keys() == {"k"}


async def test_memory_transaction_close_is_idempotent():
    client = MemoryClient()
    transaction = await _MemoryTransaction(client).begin()
    await transaction.commit()
    await transaction.commit()
    await transaction.rollback()
    assert True
