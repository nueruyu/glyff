from typing import Callable

from glyff import ExecutionRepository

StoreFactory = Callable[[str], ExecutionRepository]
