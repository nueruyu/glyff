import pytest

from glyff import ArgsHasher, Session, engrave
from glyff._context import Context
from glyff._executor import execute
from glyff._models import ExecutionId
from glyff.tests.stubs.store import StubSessionStore
from glyff.tests.types import StoreFactory

_calls: list[str] = []
_interrupt_root = False


class RootInterrupted(Exception):
    pass


@pytest.fixture(autouse=True)
def reset_state():
    global _interrupt_root
    _calls.clear()
    _interrupt_root = False


@engrave
async def durable_child() -> str:
    _calls.append("child")
    return "child"


@engrave
async def interrupting_root() -> str:
    _calls.append("root")
    value = await durable_child()
    if _interrupt_root:
        raise RootInterrupted()
    return value


async def test_completed_child_is_reused_after_parent_interrupts(
    store_factory: StoreFactory, hasher: ArgsHasher
):
    global _interrupt_root
    store = store_factory("per-event-child-reuse")

    _interrupt_root = True
    with pytest.raises(RootInterrupted):
        async with Session(id="per-event-child-reuse", store=store, hasher=hasher):
            await interrupting_root()

    assert _calls == ["root", "child"]

    _calls.clear()
    _interrupt_root = False
    async with Session(id="per-event-child-reuse", store=store, hasher=hasher):
        result = await interrupting_root()

    assert result == "child"
    assert _calls == ["root"]


async def test_executor_records_without_transaction_scope(
    mock_store: StubSessionStore,
    base_execution_id: ExecutionId,
    test_context: Context,
):
    async def sample_func() -> str:
        return "ok"

    result = await execute(test_context, base_execution_id, sample_func, (), {}, str)

    assert result == "ok"
    assert not mock_store.get_calls("begin_transaction")
    assert not mock_store.get_calls("commit")
    assert not mock_store.get_calls("rollback")
