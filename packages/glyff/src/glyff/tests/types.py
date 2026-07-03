from typing import Callable

from glyff import SessionStore

StoreFactory = Callable[[str], SessionStore]
