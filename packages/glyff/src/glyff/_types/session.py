"""Persistent session identifiers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionId:
    """A non-empty, application-defined session identifier."""

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("A session id cannot be empty.")

    def __str__(self) -> str:
        return self.value
