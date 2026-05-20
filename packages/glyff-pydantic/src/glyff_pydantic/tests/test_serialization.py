import inspect

from pydantic import BaseModel

from glyff_pydantic import PydanticArgsHasher, PydanticSerializer


class MyModel(BaseModel):
    x: int
    y: str


def sample_func(a: int, b: str = "default"):
    pass


s = PydanticSerializer()
h = PydanticArgsHasher()


def test_hash_args_positional_vs_keyword_are_equal():
    sig = inspect.signature(sample_func)
    h1 = h.hash_args(sample_func, sig, (1,), {"b": "test"})
    h2 = h.hash_args(sample_func, sig, (), {"a": 1, "b": "test"})
    assert h1 == h2


def test_hash_args_defaults_are_included():
    sig = inspect.signature(sample_func)
    h1 = h.hash_args(sample_func, sig, (1,), {})
    h2 = h.hash_args(sample_func, sig, (), {"a": 1, "b": "default"})
    assert h1 == h2


def test_hash_args_different_values_differ():
    sig = inspect.signature(sample_func)
    h1 = h.hash_args(sample_func, sig, (1,), {})
    h2 = h.hash_args(sample_func, sig, (2,), {})
    assert h1 != h2


def test_hash_args_is_deterministic():
    sig = inspect.signature(sample_func)
    h1 = h.hash_args(sample_func, sig, (42,), {"b": "hello"})
    h2 = h.hash_args(sample_func, sig, (42,), {"b": "hello"})
    assert h1 == h2


def test_serialize_deserialize_primitives():
    data = {"key": "value", "num": 123, "flag": True, "items": [1, "a"]}
    assert s.deserialize(s.serialize(data, dict), dict) == data


def test_serialize_deserialize_pydantic_model():
    model = MyModel(x=42, y="hello")
    assert s.deserialize(s.serialize(model, MyModel), MyModel) == model


def test_serialize_deserialize_list_of_models():
    models = [MyModel(x=i, y=str(i)) for i in range(3)]
    result = s.deserialize(s.serialize(models, list[MyModel]), list[MyModel])
    assert result == models


def test_serialize_produces_stable_output():
    d1 = {"b": 2, "a": 1}
    d2 = {"a": 1, "b": 2}
    assert s.serialize(d1, dict) == s.serialize(d2, dict)
