import pytest

from glyff._models import ExecutionId
from glyff._sequencer import Sequencer


@pytest.mark.asyncio
async def test_sequence_is_scoped_by_args_hash() -> None:
    sequencer = Sequencer()
    parent = ExecutionId(None, "parent", 0, "parent-hash")

    first_x = await sequencer.next(parent, "child", "args-x")
    first_y = await sequencer.next(parent, "child", "args-y")
    second_x = await sequencer.next(parent, "child", "args-x")
    second_y = await sequencer.next(parent, "child", "args-y")

    assert (first_x, first_y, second_x, second_y) == (0, 0, 1, 1)


@pytest.mark.asyncio
async def test_reset_for_call_resets_all_args_hash_scoped_children() -> None:
    sequencer = Sequencer()
    parent = ExecutionId(None, "parent", 0, "parent-hash")

    await sequencer.next(parent, "child", "args-x")
    await sequencer.next(parent, "child", "args-y")

    await sequencer.reset_for_call(parent)

    assert await sequencer.next(parent, "child", "args-x") == 0
    assert await sequencer.next(parent, "child", "args-y") == 0
