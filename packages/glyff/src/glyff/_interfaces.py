from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any

from ._canonical_arguments import CanonicalArguments
from ._execution import Execution, ExecutionStatus
from ._types import DomainId, DomainVersion, ExecutionId, SessionId


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
    """Execution aggregate persistence, explicitly scoped by ``SessionId``."""

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
        descendants of that execution. Changes staged in the caller's open
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
    def canonicalize(self, arguments: Mapping[str, Any]) -> CanonicalArguments:
        """Returns the canonical arguments for one bound call."""
        ...


class Backend(ABC):
    """A bundle of persistence-related collaborators."""

    @property
    @abstractmethod
    def repository(self) -> ExecutionRepository: ...

    @property
    @abstractmethod
    def transaction_provider(self) -> TransactionProvider: ...

    @abstractmethod
    async def claim_domain(
        self,
        session_id: SessionId,
        domain_id: DomainId,
        version: DomainVersion,
    ) -> DomainVersion:
        """Records ``version`` for a domain this session carries none for, and
        returns the version the pair carries afterwards.

        One atomic step, holding across processes, so two of them declaring
        different versions cannot both find the domain unclaimed.
        """
        ...
