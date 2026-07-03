from glyff import Execution, ExecutionId, SerializedValue


def _eid(name: str) -> ExecutionId:
    return ExecutionId(parent_id=None, name=name, sequence=0, args_hash="h")


async def test_metadata_roundtrips_after_reopen(backend_factory, serializer):
    eid = _eid("root")
    backend = backend_factory("meta")
    execution = Execution.start(eid)
    execution.set_metadata(
        "trace",
        SerializedValue(await serializer.serialize({"step": 1}, dict)),
    )
    tx = await backend.transactions.begin_transaction()
    await backend.executions.save(execution)
    await tx.commit()

    reopened = backend_factory("meta")
    state = await reopened.executions.get(eid)
    assert state is not None
    metadata = state.get_metadata("trace")
    assert metadata is not None
    assert await serializer.deserialize(metadata.value.data, dict) == {"step": 1}


async def test_metadata_rollback(backend_factory, serializer):
    eid = _eid("root")
    backend = backend_factory("meta-rb")
    tx = await backend.transactions.begin_transaction()
    execution = Execution.start(eid)
    execution.set_metadata("k", SerializedValue(await serializer.serialize("v", str)))
    await backend.executions.save(execution)
    await tx.rollback()
    assert await backend.executions.get(eid) is None


async def test_delete_many_removes_metadata(backend_factory, serializer):
    eid = _eid("root")
    backend = backend_factory("meta-del")
    execution = Execution.start(eid)
    execution.set_metadata("k", SerializedValue(await serializer.serialize("v", str)))
    tx = await backend.transactions.begin_transaction()
    await backend.executions.save(execution)
    await tx.commit()

    tx = await backend.transactions.begin_transaction()
    await backend.executions.delete_many([eid])
    await tx.commit()

    assert await backend.executions.get(eid) is None
