from __future__ import annotations

from .models import ExecutionId


def _format_frame(eid: ExecutionId) -> str:
    """Formats a single ExecutionId frame into a string component."""
    return f"{eid.name}#{eid.sequence}:{eid.args_hash}"


def _parse_frame_components(frame_str: str) -> tuple[str, int, str]:
    """Parses a string component back into the parts of an ExecutionId frame."""
    name, rest = frame_str.split("#", 1)
    seq_str, args_hash = rest.split(":", 1)
    return name, int(seq_str), args_hash


def execution_id_to_path(eid: ExecutionId) -> str:
    """Converts an ExecutionId into a full, slash-separated path string.

    The path reflects the call hierarchy, ensuring global uniqueness and enabling
    prefix-based searches for descendants.
    """
    frames: list[str] = []
    current: ExecutionId | None = eid
    while current is not None:
        frames.append(_format_frame(current))
        current = current.parent_id
    frames.reverse()
    return "/".join(frames)


def path_to_execution_id(path: str) -> ExecutionId:
    """Reconstructs an ExecutionId and its parent chain from a path string."""
    if not path:
        raise ValueError("Cannot rebuild ExecutionId from an empty path.")

    parent: ExecutionId | None = None
    eid: ExecutionId | None = None
    for frame_str in path.split("/"):
        name, sequence, args_hash = _parse_frame_components(frame_str)
        eid = ExecutionId(
            parent_id=parent, name=name, sequence=sequence, args_hash=args_hash
        )
        parent = eid
    if eid is None:
        raise ValueError("Cannot rebuild ExecutionId from an empty path.")
    return eid
