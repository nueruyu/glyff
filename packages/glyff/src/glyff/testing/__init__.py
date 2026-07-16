"""Test doubles, contracts, and reference handlers for testing against glyff.

This is a public, supported surface: applications building on glyff — and, in
particular, third parties implementing their own :class:`~glyff.Backend` — can
use it in their own test suites, and the workspace packages' tests use it too.
It gives shared test infrastructure a collision-free import path
(``glyff.testing``) that ships as real library surface rather than living
inside an importable test tree.

The backend conformance suite lets a storage backend verify it honours the
glyff contract by subclassing the relevant bases and supplying a
``backend_factory`` fixture::

    from glyff.testing import DurableBackendContract, ExecutionBackendContract

    class TestMyBackend(ExecutionBackendContract, DurableBackendContract):
        @pytest.fixture
        def backend_factory(self):
            def factory(session_id: str):
                return MyBackend(...)

            return factory

Importing this module requires ``pytest`` (install ``glyff[testing]``); the
conformance contracts are pytest test classes.
"""

from ._backend_contract import (
    BackendFactory,
    BackendHandle,
    BinarySafeBackendContract,
    DurableBackendContract,
    ExecutionBackendContract,
    TextBackendContract,
    eid,
    save_execution,
    value,
)
from ._pruning import PruningEventHandler

__all__ = [
    "BackendFactory",
    "BackendHandle",
    "BinarySafeBackendContract",
    "DurableBackendContract",
    "ExecutionBackendContract",
    "TextBackendContract",
    "PruningEventHandler",
    "eid",
    "save_execution",
    "value",
]
