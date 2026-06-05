from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

# Separator between frames in an ExecutionId's canonical key (see ExecutionId
# for the per-frame format and the invariants that keep it reversible).
_KEY_SEP = "/"


@dataclass(frozen=True)
class ExecutionId:
    """
    A unique, deterministic identifier for a task call.
    It forms a hierarchy through the 'parent_id' attribute.
    """

    parent_id: ExecutionId | None
    name: str
    sequence: int
    args_hash: str

    def __str__(self) -> str:
        """
        Generates a human-readable representation for debugging purposes only.
        This format is NOT the persistence key; use ``to_key`` for that.
        """
        parent_info = (
            f", parent='{self.parent_id.name}#{self.parent_id.sequence}'"
            if self.parent_id
            else ""
        )
        return f"ExecutionId(name='{self.name}', sequence={self.sequence}{parent_info})"

    # ------------------------------------------------------------------
    # Canonical key codec
    # ------------------------------------------------------------------
    # An ExecutionId is encoded as a path of *frames*, outermost → innermost,
    # joined by ``_KEY_SEP``. Each frame is ``"{name}#{sequence}:{args_hash}"``.
    # Stores build their persistence keys from these methods, so the encoding
    # lives on the identity itself rather than being re-derived per store.
    #
    # Invariants that keep the encoding reversible:
    #   - ``name`` contains none of '#', ':', '/' (it is an identifier / dotted
    #     path chosen by @engrave).
    #   - ``args_hash`` is a hex digest, so it contains none of them either,
    #     making the first '#' and ':' in a frame unambiguous separators.
    #   - ``sequence`` is a non-negative base-10 int.

    def to_frame(self) -> str:
        """This id alone (ignoring ancestry) encoded as one frame."""
        return f"{self.name}#{self.sequence}:{self.args_hash}"

    def to_frames(self) -> list[str]:
        """Full ancestry as frames, outermost → innermost."""
        frames: list[str] = []
        current: ExecutionId | None = self
        while current is not None:
            frames.append(current.to_frame())
            current = current.parent_id
        frames.reverse()
        return frames

    def to_key(self) -> str:
        """Full ancestry as a single, globally-unique key string.

        Globally unique (ancestry is included, and ``sequence`` restarts per
        parent) and prefix-structured: every strict descendant's key begins
        with ``descendant_key_prefix``."""
        return _KEY_SEP.join(self.to_frames())

    def descendant_key_prefix(self) -> str:
        """Key prefix shared by every strict descendant of this id."""
        return self.to_key() + _KEY_SEP

    @classmethod
    def from_frames(cls, frames: list[str]) -> ExecutionId:
        """Rebuild the chain from frames (outermost → innermost).

        Inverse of :meth:`to_frames`."""
        parent: ExecutionId | None = None
        eid: ExecutionId | None = None
        for frame in frames:
            name, rest = frame.split("#", 1)
            seq_str, args_hash = rest.split(":", 1)
            eid = cls(
                parent_id=parent,
                name=name,
                sequence=int(seq_str),
                args_hash=args_hash,
            )
            parent = eid
        if eid is None:
            raise ValueError("Cannot rebuild an ExecutionId from an empty frame list")
        return eid

    @classmethod
    def from_key(cls, key: str) -> ExecutionId:
        """Inverse of :meth:`to_key`."""
        return cls.from_frames(key.split(_KEY_SEP))


class ExecutionStatus(Enum):
    """Represents the lifecycle state of a task."""

    STARTED = auto()
    COMPLETED = auto()
    FAILED = auto()


@dataclass(frozen=True)
class ExecutionRecord:
    """Represents the persisted state and outcome of a single execution."""

    status: ExecutionStatus
    result: Any | None = None
    error: str | None = None
