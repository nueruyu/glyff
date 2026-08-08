"""Public test helpers for building against glyff.

A supported surface: applications on glyff — and third parties implementing a
:class:`~glyff.Backend`, a :class:`~glyff.Serializer` or an
:class:`~glyff.ArgumentCanonicalizer` — can use it in their own suites, as the
workspace packages do. Subclass the contracts that apply and supply a factory::

    import pytest

    from glyff.testing import EngravedCallContract, ExecutionBackendContract

    class TestMyBackend(ExecutionBackendContract, EngravedCallContract):
        @pytest.fixture
        def backend_factory(self):
            def factory(store: str):
                return MyBackend(...)

            return factory

`docs/backends.md` lists every contract and what it drives. Importing this module
needs pytest (install ``glyff[testing]``).
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
