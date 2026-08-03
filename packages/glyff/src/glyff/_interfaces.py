import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from typing import Any, Callable, Protocol

from ._models import CanonicalValue, Execution, ExecutionId, ExecutionStatus


class Transaction(ABC):
    """
    A transaction context for a TransactionProvider.
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


class TransactionProvider(ABC):
    """Provides transactions for TransactionScope."""

    @abstractmethod
    async def begin_transaction(self) -> Transaction:
        """Begins a transaction and returns a transaction object."""
        ...


class ExecutionRepository(ABC):
    """Repository for Execution aggregates."""

    @abstractmethod
    async def get(self, execution_id: ExecutionId) -> Execution | None: ...

    @abstractmethod
    async def save(self, execution: Execution) -> None: ...

    @abstractmethod
    def executions(
        self,
        *,
        status: ExecutionStatus | None = None,
        under: ExecutionId | None = None,
    ) -> AsyncIterator[Execution]:
        """Yields the stored executions, each after its own ancestors.

        ``status`` keeps only executions in that state, ``under`` only the strict
        descendants of that execution. Records staged in the caller's open
        transaction are included.
        """
        ...

    @abstractmethod
    async def delete_many(self, execution_ids: Iterable[ExecutionId]) -> None: ...


class AppVersionStore(ABC):
    """Reads and writes the application version a store's records were written under.

    A generation marker owned by the application, unlike the store's own format
    version: glyff only records it and refuses a mismatch (see
    :class:`~glyff.exceptions.AppVersionMismatchError`).
    """

    @abstractmethod
    async def read(self) -> str | None:
        """Returns the recorded version, or ``None`` if the store carries none."""
        ...

    @abstractmethod
    async def write(self, app_version: str) -> None:
        """Stages the version into the caller's open transaction."""
        ...


class Serializer(ABC):
    """An interface for serializing/deserializing values."""

    @abstractmethod
    async def serialize(self, value: Any, type_hint: type) -> bytes:
        """Serializes a value to bytes."""
        ...

    @abstractmethod
    async def deserialize(self, data: bytes, type_hint: type) -> Any:
        """Deserializes bytes to a value of the given type."""
        ...


class ArgumentCanonicalizer(ABC):
    """An interface for normalizing a call's arguments into a canonical form.

    The canonical form is what an execution is keyed by: it is encoded once, and
    those bytes are both hashed into ``ExecutionId.arguments_digest`` and recorded on the
    execution. Canonicalizing is not serializing — it is one-way and deliberately
    lossy, keeping only what identity depends on.
    """

    @abstractmethod
    def canonicalize(
        self, func: Callable, sig: inspect.Signature, args: tuple, kwargs: dict
    ) -> CanonicalValue:
        """Normalizes a call's bound arguments into the JSON data model."""
        ...


class Backend(Protocol):
    """A bundle of persistence-related collaborators."""

    @property
    def repository(self) -> ExecutionRepository: ...

    @property
    def transaction_provider(self) -> TransactionProvider: ...

    @property
    def app_version(self) -> AppVersionStore | None:
        """``None`` for a store too ephemeral to outlive the code that wrote it."""
        ...
