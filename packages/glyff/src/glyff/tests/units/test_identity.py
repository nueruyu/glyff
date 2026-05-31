import inspect

import pytest

from glyff import identify
from glyff.serialization import JsonArgsHasher

h = JsonArgsHasher()


@identify("static-class-id")
class Decorated:
    def method(self, x: int):
        pass


class Undecorated:
    def method(self, x: int):
        pass


def test_identify_decorator_sets_attribute():
    instance = Decorated()
    assert instance.__glyff_identity__ == "static-class-id"


def test_identify_function_sets_attribute():
    instance = Undecorated()
    assert not hasattr(instance, "__glyff_identity__")
    identify(instance, "dynamic-instance-id")
    assert instance.__glyff_identity__ == "dynamic-instance-id"


def test_identify_invalid_arguments_raises():
    with pytest.raises(TypeError):
        identify(123)  # type: ignore[arg-type]


def test_identify_affects_hash():
    func = Decorated.method
    sig = inspect.signature(func)

    inst1 = Decorated()
    inst2 = Undecorated()
    identify(inst2, "static-class-id")  # same id as the decorated class
    inst3 = Undecorated()
    identify(inst3, "different-id")

    hash1 = h.hash_args(func, sig, (inst1, 1), {})
    hash2 = h.hash_args(func, sig, (inst2, 1), {})
    hash3 = h.hash_args(func, sig, (inst3, 1), {})

    assert hash1 == hash2
    assert hash1 != hash3


def test_property_identity_is_dynamic():
    class Dynamic:
        def __init__(self, value: str):
            self._value = value

        @property
        def __glyff_identity__(self) -> str:
            return self._value

        def method(self, x: int):
            pass

    func = Dynamic.method
    sig = inspect.signature(func)

    same_a = h.hash_args(func, sig, (Dynamic("a"), 1), {})
    same_b = h.hash_args(func, sig, (Dynamic("a"), 1), {})
    other = h.hash_args(func, sig, (Dynamic("b"), 1), {})

    assert same_a == same_b
    assert same_a != other
