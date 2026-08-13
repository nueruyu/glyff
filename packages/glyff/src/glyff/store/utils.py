from __future__ import annotations

from urllib.parse import quote, unquote

from .._types import ArgumentsDigest, DomainId, ExecutionId, ExecutionName


def _encode(value: str) -> str:
    """Percent-encodes one frame component."""
    return quote(value, safe="")


def _decode(encoded: str) -> str:
    """Reverses :func:`_encode`, accepting only canonical encodings.

    ``unquote`` is permissive — it leaves a malformed ``%`` sequence alone and
    accepts escapes that need not have been escaped, so two different paths
    could decode to one identity. Re-encoding and comparing refuses that.
    """
    decoded = unquote(encoded)
    if _encode(decoded) != encoded:
        raise ValueError(f"{encoded!r} is not a canonically encoded path component.")
    return decoded


def _decode_sequence(spelling: str) -> int:
    """Reads an ordinal, accepting only the spelling :func:`_format_frame` writes.

    ``int`` takes a sign, leading zeroes, surrounding whitespace and underscore
    separators, so ``#1``, ``#01`` and ``#+1`` would be three paths for one
    identity. Formatting the result back and comparing leaves exactly one.
    """
    try:
        sequence = int(spelling)
    except ValueError:
        raise ValueError(f"{spelling!r} is not an execution ordinal.") from None
    if sequence < 0 or str(sequence) != spelling:
        raise ValueError(f"{spelling!r} is not a canonical execution ordinal.")
    return sequence


def _format_frame(eid: ExecutionId) -> str:
    """Formats a single ExecutionId frame into a string component."""
    return (
        f"{_encode(eid.domain_id.value)}:{_encode(eid.name.value)}"
        f"#{eid.sequence}:{_encode(eid.arguments_digest.value)}"
    )


def _parse_frame_components(
    frame_str: str,
) -> tuple[DomainId, ExecutionName, int, ArgumentsDigest]:
    """Parses a string component back into the parts of an ExecutionId frame."""
    identity, _, ordinal = frame_str.partition("#")
    if not ordinal:
        raise ValueError(f"{frame_str!r} is not an execution path frame.")

    domain, separator, name = identity.partition(":")
    if not separator:
        raise ValueError(f"{frame_str!r} is not an execution path frame.")

    sequence, separator, digest = ordinal.partition(":")
    if not separator:
        raise ValueError(f"{frame_str!r} is not an execution path frame.")

    return (
        DomainId(_decode(domain)),
        ExecutionName(_decode(name)),
        _decode_sequence(sequence),
        ArgumentsDigest(_decode(digest)),
    )


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
        domain_id, name, sequence, digest = _parse_frame_components(frame_str)
        eid = ExecutionId(
            parent_id=parent,
            domain_id=domain_id,
            name=name,
            sequence=sequence,
            arguments_digest=digest,
        )
        parent = eid
    if eid is None:
        raise ValueError("Cannot rebuild ExecutionId from an empty path.")
    return eid
