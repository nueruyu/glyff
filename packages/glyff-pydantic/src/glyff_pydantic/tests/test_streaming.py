from collections.abc import AsyncIterator

import pytest
from glyff import Session, engrave
from glyff.stores import MemoryClient, MemorySessionStore
from pydantic import BaseModel

from glyff_pydantic import PydanticArgsHasher, PydanticSerializer


class Token(BaseModel):
    index: int
    text: str


_runs: list[int] = []


@pytest.fixture(autouse=True)
def reset_state():
    _runs.clear()
    yield
    _runs.clear()


@engrave
async def stream_tokens(n: int) -> AsyncIterator[Token]:
    _runs.append(n)
    for i in range(n):
        yield Token(index=i, text=f"t{i}")


async def test_streaming_pydantic_models_round_trip_on_replay():
    """The collected items are serialized as `list[Token]`, so a completed stream
    of Pydantic models replays as validated model instances without re-running."""
    serializer = PydanticSerializer()
    hasher = PydanticArgsHasher()
    store = MemorySessionStore(client=MemoryClient(), serializer=serializer)

    async with Session(id="pyd-stream", store=store, hasher=hasher):
        first = [t async for t in stream_tokens(3)]
    assert first == [Token(index=i, text=f"t{i}") for i in range(3)]
    assert _runs == [3]

    _runs.clear()
    async with Session(id="pyd-stream", store=store, hasher=hasher):
        second = [t async for t in stream_tokens(3)]
    # Replayed from store: validated back into Token instances, body not re-run.
    assert all(isinstance(t, Token) for t in second)
    assert second == [Token(index=i, text=f"t{i}") for i in range(3)]
    assert _runs == []
