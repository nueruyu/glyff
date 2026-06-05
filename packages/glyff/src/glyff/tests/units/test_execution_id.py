import pytest

from glyff import ExecutionId


def _child(parent, name, seq=0, h="h0") -> ExecutionId:
    return ExecutionId(parent_id=parent, name=name, sequence=seq, args_hash=h)


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


# --- canonical key codec -------------------------------------------------


def test_frames_roundtrip():
    eid = _child(_child(_child(None, "root", 1, "a"), "mid", 2, "bc"), "leaf", 5, "def456")
    assert ExecutionId.from_frames(eid.to_frames()) == eid


def test_key_roundtrip():
    eid = _child(_child(None, "root", 0, "aa"), "leaf", 7, "ff00")
    assert ExecutionId.from_key(eid.to_key()) == eid


def test_frames_are_outermost_first():
    eid = _child(_child(None, "root"), "leaf")
    frames = eid.to_frames()
    assert frames[0].startswith("root#")
    assert frames[-1].startswith("leaf#")
    assert eid.to_key() == "/".join(frames)


def test_descendant_prefix_is_key_plus_separator():
    parent = _child(None, "root")
    child = _child(parent, "child")
    prefix = parent.descendant_key_prefix()
    assert prefix == parent.to_key() + "/"
    # A genuine child's key starts with the prefix; the parent's own does not.
    assert child.to_key().startswith(prefix)
    assert not parent.to_key().startswith(prefix)


def test_empty_frames_rejected():
    with pytest.raises(ValueError):
        ExecutionId.from_frames([])
