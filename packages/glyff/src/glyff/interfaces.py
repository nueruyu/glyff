import inspect
from abc import ABC, abstractmethod
from typing import Any, Callable

from .models import ExecutionId, ExecutionRecord


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

    @abstractmethod
    async def get_descendants(
        self, execution_id: ExecutionId
    ) -> list[ExecutionId]:
        """
        Returns the ExecutionIds that are *strict* descendants of the given one,
        based on the records currently held by this store.

        This is a read-only structural query over the store's own data; it
        carries no pruning policy. Callers decide what to do with the result.
        """
        ...

    @abstractmethod
    async def delete_execution(self, execution_id: ExecutionId) -> None:
        """
        Deletes the record(s) for exactly the given execution.

        Deletion is staged within the current transaction and applied on commit
        (and discarded on rollback), mirroring how writes are staged. The store
        only deletes the single execution it is given; it has no notion of
        children, descendants, or pruning.
        """
        ...
