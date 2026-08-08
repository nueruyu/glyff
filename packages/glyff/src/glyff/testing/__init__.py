"""Public test helpers for building against glyff.

A supported surface: applications on glyff — and third parties implementing
their own :class:`~glyff.Backend` — can use it in their own test suites, as the
workspace packages' tests do. A backend checks it honours the glyff contract by
subclassing the relevant contract bases and supplying a ``backend_factory``
fixture::

    import pytest

    from glyff.testing import DurableBackendContract, ExecutionBackendContract

    class TestMyBackend(ExecutionBackendContract, DurableBackendContract):
        @pytest.fixture
        def backend_factory(self):
            def factory(session_id: str):
                return MyBackend(...)

            return factory

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
