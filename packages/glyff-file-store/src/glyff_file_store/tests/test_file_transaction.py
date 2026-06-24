import asyncio

from glyff_file_store._store import _FileTransaction


async def test_file_transaction_concurrent_close_finishes_once():
    class FakeStore:
        def __init__(self):
            self.calls: list[str] = []
            self.end_calls = 0
            self.commit_started = asyncio.Event()
            self.release_commit = asyncio.Event()

        def begin_staging(self):
            return object()

        def end_staging(self, token) -> None:
            self.end_calls += 1

        async def _commit_current(self) -> None:
            self.calls.append("commit")
            self.commit_started.set()
            await self.release_commit.wait()

        async def _rollback_current(self) -> None:
            self.calls.append("rollback")

    store = FakeStore()
    transaction = _FileTransaction(store)  # type: ignore[arg-type]

    commit_task = asyncio.create_task(transaction.commit())
    await store.commit_started.wait()

    rollback_task = asyncio.create_task(transaction.rollback())
    await asyncio.sleep(0)
    store.release_commit.set()

    await asyncio.gather(commit_task, rollback_task)

    assert store.calls == ["commit"]
    assert store.end_calls == 1
