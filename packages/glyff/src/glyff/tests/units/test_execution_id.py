from glyff import ExecutionId


def test_str_representation_no_parent():
    eid = ExecutionId(parent_id=None, name="my.func", sequence=0, args_hash="hash1")
    assert str(eid) == "ExecutionId(name='my.func', sequence=0)"


def test_str_representation_with_parent():
    parent = ExecutionId(parent_id=None, name="root", sequence=1, args_hash="hash_p")
    eid = ExecutionId(
        parent_id=parent, name="child.func", sequence=0, args_hash="hash_c"
    )
    expected = "ExecutionId(name='child.func', sequence=0, parent='root#1')"
    assert str(eid) == expected
