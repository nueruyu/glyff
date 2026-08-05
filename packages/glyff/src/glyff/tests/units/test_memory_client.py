"""Unit tests for ``MemoryClient`` read-your-writes consistency: reads within
an uncommitted transaction must observe the staged view, consistent with
``all_keys()``."""

import pytest

from glyff.store._memory_client import MemoryClient, MemoryKey
from glyff.store._memory import _MemoryTransaction


async def _commit_write(client: MemoryClient, key: MemoryKey, value: bytes) -> None:
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
        client.stage_write(("s", "k"), b"v")
        assert await client.read(("s", "k")) == b"v"
    finally:
        client.end_staging(token)


async def test_staged_write_overrides_committed_value():
    client = MemoryClient()
    await _commit_write(client, ("s", "k"), b"old")

    token, _ = client.begin_staging()
    try:
        client.stage_write(("s", "k"), b"new")
        assert await client.read(("s", "k")) == b"new"
        assert client.data[("s", "k")] == b"old"
    finally:
        client.end_staging(token)


async def test_read_observes_staged_delete():
    client = MemoryClient()
    await _commit_write(client, ("s", "k"), b"v")

    token, _ = client.begin_staging()
    try:
        client.stage_delete(("s", "k"))
        assert await client.read(("s", "k")) is None
    finally:
        client.end_staging(token)


async def test_read_falls_through_to_committed_value():
    client = MemoryClient()
    await _commit_write(client, ("s", "k"), b"v")
    assert await client.read(("s", "k")) == b"v"


async def test_clear_staged_discards_staged_view():
    client = MemoryClient()
    token, _ = client.begin_staging()
    try:
        client.stage_write(("s", "k"), b"v")
        client.clear_staged()
        assert await client.read(("s", "k")) is None
    finally:
        client.end_staging(token)


async def test_read_consistent_with_all_keys():
    client = MemoryClient()
    await _commit_write(client, ("s", "committed"), b"c")

    token, _ = client.begin_staging()
    try:
        client.stage_write(("s", "staged"), b"s")
        client.stage_delete(("s", "committed"))

        assert client.all_keys() == {("s", "staged")}
        assert await client.read(("s", "staged")) == b"s"
        assert await client.read(("s", "committed")) is None
    finally:
        client.end_staging(token)


async def test_read_staged_false_returns_committed_ignoring_staged():
    client = MemoryClient()
    await _commit_write(client, ("s", "k"), b"committed")

    token, _ = client.begin_staging()
    try:
        client.stage_write(("s", "k"), b"staged")
        assert await client.read(("s", "k")) == b"staged"
        assert await client.read(("s", "k"), staged=False) == b"committed"

        client.stage_delete(("s", "k"))
        assert await client.read(("s", "k")) is None
        assert await client.read(("s", "k"), staged=False) == b"committed"
    finally:
        client.end_staging(token)


async def test_write_operations_require_staging():
    client = MemoryClient()

    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_write(("s", "k"), b"v")
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.stage_delete(("s", "k"))
    with pytest.raises(RuntimeError, match="outside a transaction"):
        client.clear_staged()
    with pytest.raises(RuntimeError, match="outside a transaction"):
        await client.commit_staged()


async def test_all_keys_without_staging_returns_committed_keys():
    client = MemoryClient()
    await _commit_write(client, ("s", "k"), b"v")

    assert client.all_keys() == {("s", "k")}


async def test_memory_transaction_close_is_idempotent():
    client = MemoryClient()
    transaction = await _MemoryTransaction(client).begin()
    await transaction.commit()
    await transaction.commit()
    await transaction.rollback()
    assert True
