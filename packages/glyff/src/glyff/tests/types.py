from typing import Callable

from glyff.interfaces import SessionStore

StoreFactory = Callable[[str], SessionStore]
