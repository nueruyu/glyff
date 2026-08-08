import asyncio

from glyff import ArgumentsDigest, DomainId, ExecutionId, ExecutionName
from glyff._sequencer import Sequencer

D = DomainId("test")

# A fixed args hash so these tests exercise the (parent, name) dimensions of the
# counter while holding the content scope constant. The args-hash dimension is
# covered separately in test_parallel_safe_identity.py.
_H = ArgumentsDigest("args-hash")


async def test_increments_per_name():
    s = Sequencer()
    assert await s.next(None, D, ExecutionName("func_a"), _H) == 0
    assert await s.next(None, D, ExecutionName("func_a"), _H) == 1
    assert await s.next(None, D, ExecutionName("func_b"), _H) == 0
    assert await s.next(None, D, ExecutionName("func_a"), _H) == 2


async def test_parent_scopes_are_independent():
    s = Sequencer()
    parent_x = ExecutionId(
        None, D, ExecutionName("parent_x"), 0, ArgumentsDigest("hash1")
    )
    parent_y = ExecutionId(
        None, D, ExecutionName("parent_y"), 0, ArgumentsDigest("hash2")
    )
    assert await s.next(parent_x, D, ExecutionName("child"), _H) == 0
    assert await s.next(parent_y, D, ExecutionName("child"), _H) == 0
    assert await s.next(parent_x, D, ExecutionName("child"), _H) == 1


async def test_concurrency_produces_unique_values():
    s = Sequencer()
    results = await asyncio.gather(
        *[s.next(None, D, ExecutionName("func"), _H) for _ in range(100)]
    )
    assert sorted(results) == list(range(100))


async def test_reset_for_call_restarts_child_sequence():
    s = Sequencer()
    parent = ExecutionId(None, D, ExecutionName("parent"), 0, ArgumentsDigest("hash"))
    await s.next(parent, D, ExecutionName("child"), _H)
    await s.next(parent, D, ExecutionName("child"), _H)
    assert await s.next(parent, D, ExecutionName("child"), _H) == 2

    await s.reset_for_call(parent)
    assert await s.next(parent, D, ExecutionName("child"), _H) == 0


async def test_reset_does_not_affect_other_parents():
    s = Sequencer()
    p1 = ExecutionId(None, D, ExecutionName("p1"), 0, ArgumentsDigest("first"))
    p2 = ExecutionId(None, D, ExecutionName("p2"), 0, ArgumentsDigest("second"))
    await s.next(p1, D, ExecutionName("child"), _H)
    await s.next(p2, D, ExecutionName("child"), _H)

    await s.reset_for_call(p1)
    assert await s.next(p2, D, ExecutionName("child"), _H) == 1


async def test_sequence_is_scoped_by_arguments_digest() -> None:
    sequencer = Sequencer()
    parent = ExecutionId(
        None, D, ExecutionName("parent"), 0, ArgumentsDigest("parent-hash")
    )

    first_x = await sequencer.next(
        parent, D, ExecutionName("child"), ArgumentsDigest("args-x")
    )
    first_y = await sequencer.next(
        parent, D, ExecutionName("child"), ArgumentsDigest("args-y")
    )
    second_x = await sequencer.next(
        parent, D, ExecutionName("child"), ArgumentsDigest("args-x")
    )
    second_y = await sequencer.next(
        parent, D, ExecutionName("child"), ArgumentsDigest("args-y")
    )

    assert (first_x, first_y, second_x, second_y) == (0, 0, 1, 1)


async def test_reset_for_call_resets_all_digest_scoped_children() -> None:
    sequencer = Sequencer()
    parent = ExecutionId(
        None, D, ExecutionName("parent"), 0, ArgumentsDigest("parent-hash")
    )

    await sequencer.next(parent, D, ExecutionName("child"), ArgumentsDigest("args-x"))
    await sequencer.next(parent, D, ExecutionName("child"), ArgumentsDigest("args-y"))

    await sequencer.reset_for_call(parent)

    assert (
        await sequencer.next(
            parent, D, ExecutionName("child"), ArgumentsDigest("args-x")
        )
        == 0
    )
    assert (
        await sequencer.next(
            parent, D, ExecutionName("child"), ArgumentsDigest("args-y")
        )
        == 0
    )


async def test_domains_do_not_share_a_counter() -> None:
    # Two libraries can name a function the same thing; their ordinals are their
    # own, or one would number the other's calls.
    sequencer = Sequencer()
    other = DomainId("other")

    assert await sequencer.next(None, D, ExecutionName("task"), _H) == 0
    assert await sequencer.next(None, other, ExecutionName("task"), _H) == 0
    assert await sequencer.next(None, D, ExecutionName("task"), _H) == 1
