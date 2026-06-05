"""Canonical codec between :class:`ExecutionId` and its string encodings.

An ``ExecutionId`` chain is encoded as a *path* of *frames*, outermost →
innermost, joined by :data:`_PATH_SEP`. Each frame encodes one ``ExecutionId``
(ignoring ancestry) as ``"{name}{_NAME_SEP}{sequence}{_SEQ_SEP}{args_hash}"``.

This is the single place that knows that encoding. Both the in-memory and the
file store build their persistence keys from these helpers, so the format lives
in one module instead of being re-derived per store.

Invariants the codec relies on (and that the rest of glyff upholds):

- ``name`` never contains :data:`_NAME_SEP` (``"#"``), :data:`_SEQ_SEP`
  (``":"``) or :data:`_PATH_SEP` (``"/"``) — names are Python identifiers /
  dotted paths chosen by the ``@engrave`` decorator.
- ``args_hash`` is a hex digest, so it contains none of those separators
  either. This makes the *first* ``"#"`` and ``":"`` in a frame unambiguous
  separators and ``"/"`` a safe frame separator.
- ``sequence`` is a non-negative int rendered in base 10.
"""

from __future__ import annotations

from .models import ExecutionId

_NAME_SEP = "#"
_SEQ_SEP = ":"
_PATH_SEP = "/"


def execution_id_to_frame(execution_id: ExecutionId) -> str:
    """Encode a single ``ExecutionId`` (ignoring ancestry) as one frame."""
    return (
        f"{execution_id.name}{_NAME_SEP}{execution_id.sequence}"
        f"{_SEQ_SEP}{execution_id.args_hash}"
    )


def _frame_to_execution_id(frame: str, parent: ExecutionId | None) -> ExecutionId:
    """Decode one frame into an ``ExecutionId`` parented at ``parent``."""
    name, rest = frame.split(_NAME_SEP, 1)
    seq_str, args_hash = rest.split(_SEQ_SEP, 1)
    return ExecutionId(
        parent_id=parent, name=name, sequence=int(seq_str), args_hash=args_hash
    )


def execution_id_to_frames(execution_id: ExecutionId) -> list[str]:
    """Encode an ``ExecutionId``'s full ancestry as frames (outermost → innermost)."""
    frames: list[str] = []
    current: ExecutionId | None = execution_id
    while current is not None:
        frames.append(execution_id_to_frame(current))
        current = current.parent_id
    frames.reverse()
    return frames


def frames_to_execution_id(frames: list[str]) -> ExecutionId:
    """Rebuild the full ``ExecutionId`` chain from frames (outermost → innermost).

    Inverse of :func:`execution_id_to_frames`."""
    parent: ExecutionId | None = None
    eid: ExecutionId | None = None
    for frame in frames:
        eid = _frame_to_execution_id(frame, parent)
        parent = eid
    if eid is None:
        raise ValueError("Cannot rebuild an ExecutionId from an empty frame list")
    return eid


def frames_to_path(frames: list[str]) -> str:
    """Join frames (outermost → innermost) into a single path string."""
    return _PATH_SEP.join(frames)


def execution_id_to_path(execution_id: ExecutionId) -> str:
    """Encode an ``ExecutionId``'s full ancestry as a single path string.

    Globally unique (ancestry is included, and ``sequence`` restarts per
    parent) and prefix-structured: every strict descendant's path begins with
    this path followed by :data:`_PATH_SEP`."""
    return frames_to_path(execution_id_to_frames(execution_id))


def path_to_execution_id(path: str) -> ExecutionId:
    """Inverse of :func:`execution_id_to_path`."""
    return frames_to_execution_id(path.split(_PATH_SEP))


def execution_id_to_descendant_prefix(execution_id: ExecutionId) -> str:
    """Path prefix shared by every strict descendant of ``execution_id``."""
    return execution_id_to_path(execution_id) + _PATH_SEP
