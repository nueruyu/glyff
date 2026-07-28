"""Public test helpers for building against glyff.

A supported surface: applications on glyff — and third parties implementing
their own :class:`~glyff.Backend` — can use it in their own test suites, as the
workspace packages' tests do. A backend checks it honours the glyff contract by
subclassing the relevant contract bases and supplying a ``backend_factory``
fixture::

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
    DurableBackendContract,
    ExecutionBackendContract,
    TextBackendContract,
)
from ._pruning import PruningEventHandler

__all__ = [
    "BinarySafeBackendContract",
    "DurableBackendContract",
    "ExecutionBackendContract",
    "TextBackendContract",
    "PruningEventHandler",
]
