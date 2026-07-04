"""The ExecutionId <-> path encoding that the backends key records by.

descendants_of reconstructs ids from stored path keys, so the encoding must
round-trip exactly and must distinguish otherwise-identical frames that sit
under different parents.
"""

from glyff import ExecutionId
from glyff.store.utils import execution_id_to_path, path_to_execution_id


def _eid(
    name: str,
    *,
    parent: ExecutionId | None = None,
    sequence: int = 0,
    args_hash: str = "h",
) -> ExecutionId:
    return ExecutionId(
        parent_id=parent, name=name, sequence=sequence, args_hash=args_hash
    )


def test_execution_id_path_roundtrips_through_the_hierarchy():
    root = _eid("root", sequence=1, args_hash="r")
    mid = _eid("mid", parent=root, sequence=2, args_hash="abc")
    leaf = _eid("leaf", parent=mid, sequence=5, args_hash="def456")

    assert path_to_execution_id(execution_id_to_path(leaf)) == leaf


def test_same_frame_under_different_parents_have_distinct_paths():
    p1 = _eid("p1")
    p2 = _eid("p2")
    a = _eid("leaf", parent=p1, args_hash="same")
    b = _eid("leaf", parent=p2, args_hash="same")

    assert execution_id_to_path(a) != execution_id_to_path(b)
