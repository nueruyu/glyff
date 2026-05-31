import inspect
from abc import ABC, abstractmethod
from typing import Any, Callable, Final, Protocol, runtime_checkable

from .models import ExecutionId, ExecutionRecord


@runtime_checkable
class Identifiable(Protocol):
    """A protocol for objects whose identity contributes to argument hashing.

    Implement by exposing a ``__glyff_identity__`` attribute on a class to make
    its instances (or the class itself, for classmethods) usable as the receiver
    of a hashed method call. The attribute may be a plain value for a static
    identity, or a ``property`` for one computed from instance state. Use
    :func:`glyff.identify` to attach one without writing it by hand.
    """

    __glyff_identity__: Any


IDENTITY_ATTR: Final[str] = "__glyff_identity__"


class Transaction(ABC):
    """
    A transaction context for a SessionStore.
    Actual commit/rollback logic is delegated to this object.
    """

    @abstractmethod
    async def commit(self) -> None:
        """Commits the transaction."""
        ...

    @abstractmethod
    async def rollback(self) -> None:
        """Rolls back the transaction."""
        ...


class Execution(ABC):
    """
    Represents a single task execution, handling its outcome.
    """

    @abstractmethod
    async def complete(self, value: Any, return_type: type) -> None:
        """Marks the task as successfully completed with a result."""
        ...

    @abstractmethod
    async def fail(self, error: str) -> None:
        """Marks the task as failed with an error message."""
        ...


class Serializer(ABC):
    """An interface for serializing/deserializing values."""

    @abstractmethod
    def serialize(self, value: Any, type_hint: type) -> bytes:
        """Serializes a value to bytes."""
        ...

    @abstractmethod
    def deserialize(self, data: bytes, type_hint: type) -> Any:
        """Deserializes bytes to a value of the given type."""
        ...


class ArgsHasher(ABC):
    """An interface for creating a deterministic hash from function arguments."""

    @abstractmethod
    def hash_args(
        self, func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
    ) -> str:
        """Creates a deterministic hash from a function's arguments."""
        ...


class SessionStore(ABC):
    """
    Protocol for a store that persists the state and results of task calls.
    """

    @abstractmethod
    async def begin_transaction(self) -> Transaction:
        """Begins a transaction and returns a transaction object."""
        ...

    @abstractmethod
    async def start_execution(self, execution_id: ExecutionId) -> Execution:
        """
        Records that a task has started and returns an execution object
        to manage its outcome.
        """
        ...

    @abstractmethod
    async def get_execution_record(
        self, execution_id: ExecutionId, return_type: type
    ) -> ExecutionRecord | None:
        """
        Gets the persisted state of a task.
        The result, if any, is deserialized to the given type.
        """
        ...
