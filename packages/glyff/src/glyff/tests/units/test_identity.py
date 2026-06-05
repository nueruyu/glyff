"""Unit tests for the canonical ExecutionId codec in ``glyff.identity``."""

import pytest

from glyff import ExecutionId
from glyff.identity import (
    execution_id_to_descendant_prefix,
    execution_id_to_frames,
    execution_id_to_path,
    frames_to_execution_id,
    frames_to_path,
    path_to_execution_id,
)


def _child(parent, name, seq=0, h="h0") -> ExecutionId:
    return ExecutionId(parent_id=parent, name=name, sequence=seq, args_hash=h)


def test_frames_roundtrip():
    eid = _child(_child(_child(None, "root", 1, "a"), "mid", 2, "bc"), "leaf", 5, "def456")
    assert frames_to_execution_id(execution_id_to_frames(eid)) == eid


def test_path_roundtrip():
    eid = _child(_child(None, "root", 0, "aa"), "leaf", 7, "ff00")
    assert path_to_execution_id(execution_id_to_path(eid)) == eid


def test_frames_are_outermost_first():
    eid = _child(_child(None, "root"), "leaf")
    frames = execution_id_to_frames(eid)
    assert frames[0].startswith("root#")
    assert frames[-1].startswith("leaf#")
    assert execution_id_to_path(eid) == frames_to_path(frames)


def test_descendant_prefix_is_path_plus_separator():
    parent = _child(None, "root")
    child = _child(parent, "child")
    prefix = execution_id_to_descendant_prefix(parent)
    assert prefix == execution_id_to_path(parent) + "/"
    # A genuine child's path starts with the prefix; the parent's own does not.
    assert execution_id_to_path(child).startswith(prefix)
    assert not execution_id_to_path(parent).startswith(prefix)


def test_empty_frames_rejected():
    with pytest.raises(ValueError):
        frames_to_execution_id([])
