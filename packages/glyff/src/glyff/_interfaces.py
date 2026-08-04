import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from typing import Any, Callable, Protocol

from ._models import (
    CanonicalValue,
    Execution,
    ExecutionId,
    ExecutionStatus,
    SessionId,
)


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
    """Repository for Execution aggregates, across every session a store holds.

    Every method names the session it acts on. A store is not bound to one, so
    the session a record belongs to is never implied by which object you are
    holding.
    """

    @abstractmethod
    async def get(
        self, session_id: SessionId, execution_id: ExecutionId
    ) -> Execution | None: ...

    @abstractmethod
    async def save(self, session_id: SessionId, execution: Execution) -> None: ...

    @abstractmethod
    def executions(
        self,
        session_id: SessionId,
        *,
        status: ExecutionStatus | None = None,
        under: ExecutionId | None = None,
    ) -> AsyncIterator[Execution]:
        """Yields the session's executions, each after its own ancestors.

        ``status`` keeps only executions in that state, ``under`` only the strict
        descendants of that execution. Records staged in the caller's open
        transaction are included.
        """
        ...

    @abstractmethod
    async def delete_many(
        self, session_id: SessionId, execution_ids: Iterable[ExecutionId]
    ) -> None: ...


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

    async def claim_session(self, session_id: SessionId, app_version: str) -> str:
        """Records ``app_version`` for a session that carries none, and returns
        the version it carries afterwards — the caller's if it took the session,
        the incumbent's if it did not.

        One atomic step. Reading and then writing would let two processes
        declaring different versions both find the session unclaimed and both
        start, mixing two generations of records under whichever committed last.

        Whether a difference is fatal is not the store's call: it reports the
        winner and :class:`~glyff.Session` decides (see
        :class:`~glyff.exceptions.AppVersionMismatchError`).
        """
        ...
