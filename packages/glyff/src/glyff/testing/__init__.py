"""Public test helpers for building against glyff.

A supported surface: applications on glyff — and third parties implementing a
:class:`~glyff.Backend`, a :class:`~glyff.Serializer` or an
:class:`~glyff.ArgumentCanonicalizer` — can use it in their own test suites, as
the workspace packages' tests do. An implementation checks it honours the glyff
contract by subclassing the relevant bases and supplying a factory fixture::

    import pytest

    from glyff.testing import (
        DurableBackendContract,
        EngravedCallContract,
        ExecutionBackendContract,
    )

    class TestMyBackend(
        ExecutionBackendContract, DurableBackendContract, EngravedCallContract
    ):
        @pytest.fixture
        def backend_factory(self):
            def factory(store: str):
                return MyBackend(...)

            return factory

The factory names a *store*, and the same name reopens it. The session-level
contracts (`EngravedCallContract` and its siblings) drive whole engraved calls
rather than the repository, so they catch what only shows up once a `Session` is
composing the pieces.

The contracts are pytest test classes, so importing this module needs pytest
(install ``glyff[testing]``).
"""

from ._backend_contract import (
    BinarySafeBackendContract,
    DomainVersionContract,
    DurableBackendContract,
    ExecutionBackendContract,
    TextBackendContract,
    canonical_arguments,
    make_execution_id,
    save_execution,
    serialized_value,
)
from ._canonicalizer_contract import ArgumentCanonicalizerContract
from ._migration_contract import SessionMigrationContract
from ._pruning import PruningEventHandler
from ._scenarios import (
    BackendFactory,
    EngravedCallContract,
    ParallelContract,
    PruningContract,
    ResumeContract,
    make_session,
)
from ._serializer_contract import SerializerContract

__all__ = [
    "ArgumentCanonicalizerContract",
    "BackendFactory",
    "DomainVersionContract",
    "BinarySafeBackendContract",
    "DurableBackendContract",
    "ExecutionBackendContract",
    "TextBackendContract",
    "EngravedCallContract",
    "ParallelContract",
    "PruningContract",
    "PruningEventHandler",
    "ResumeContract",
    "SerializerContract",
    "SessionMigrationContract",
    "canonical_arguments",
    "make_session",
    "make_execution_id",
    "save_execution",
    "serialized_value",
]
