import pytest

from glyff._context import get_context
from glyff.exceptions import ContextNotSetError


def test_get_context_raises_custom_error_when_unset():
    with pytest.raises(ContextNotSetError, match="Workflow context is not set"):
        get_context()
