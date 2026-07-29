import asyncio

from glyff import ExecutionId
from glyff._sequencer import Sequencer

# A fixed args hash so these tests exercise the (parent, name) dimensions of the
# counter while holding the content scope constant. The args-hash dimension is
# covered separately in test_parallel_safe_identity.py.
_H = "args-hash"


async def test_increments_per_name():
    s = Sequencer()
    assert await s.next(None, "func_a", _H) == 0
    assert await s.next(None, "func_a", _H) == 1
    assert await s.next(None, "func_b", _H) == 0
    assert await s.next(None, "func_a", _H) == 2


async def test_parent_scopes_are_independent():
    s = Sequencer()
    parent_x = ExecutionId(None, "parent_x", 0, "hash1")
    parent_y = ExecutionId(None, "parent_y", 0, "hash2")
    assert await s.next(parent_x, "child", _H) == 0
    assert await s.next(parent_y, "child", _H) == 0
    assert await s.next(parent_x, "child", _H) == 1


async def test_concurrency_produces_unique_values():
    s = Sequencer()
    results = await asyncio.gather(*[s.next(None, "func", _H) for _ in range(100)])
    assert sorted(results) == list(range(100))


async def test_reset_for_call_restarts_child_sequence():
    s = Sequencer()
    parent = ExecutionId(None, "parent", 0, "hash")
    await s.next(parent, "child", _H)
    await s.next(parent, "child", _H)
    assert await s.next(parent, "child", _H) == 2

    await s.reset_for_call(parent)
    assert await s.next(parent, "child", _H) == 0


async def test_reset_does_not_affect_other_parents():
    s = Sequencer()
    p1 = ExecutionId(None, "p1", 0, "first")
    p2 = ExecutionId(None, "p2", 0, "second")
    await s.next(p1, "child", _H)
    await s.next(p2, "child", _H)

    await s.reset_for_call(p1)
    assert await s.next(p2, "child", _H) == 1


async def test_sequence_is_scoped_by_args_hash() -> None:
    sequencer = Sequencer()
    parent = ExecutionId(None, "parent", 0, "parent-hash")

    first_x = await sequencer.next(parent, "child", "args-x")
    first_y = await sequencer.next(parent, "child", "args-y")
    second_x = await sequencer.next(parent, "child", "args-x")
    second_y = await sequencer.next(parent, "child", "args-y")

    assert (first_x, first_y, second_x, second_y) == (0, 0, 1, 1)


async def test_reset_for_call_resets_all_args_hash_scoped_children() -> None:
    sequencer = Sequencer()
    parent = ExecutionId(None, "parent", 0, "parent-hash")

    await sequencer.next(parent, "child", "args-x")
    await sequencer.next(parent, "child", "args-y")

    await sequencer.reset_for_call(parent)

    assert await sequencer.next(parent, "child", "args-x") == 0
    assert await sequencer.next(parent, "child", "args-y") == 0
