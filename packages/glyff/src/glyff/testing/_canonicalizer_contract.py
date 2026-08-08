"""pytest conformance contract for :class:`~glyff.ArgumentCanonicalizer`.

Re-exported from :mod:`glyff.testing`, the public entry point.

What every canonicalizer promises, and no more. The *taxonomy* — which Python
value becomes which canonical form — belongs to whichever walk an implementation
uses, and implementations disagree there on purpose: the JSON canonicalizer
rejects a ``datetime`` as opaque where the Pydantic one represents it by value.
Anything that could differ that way is left to the tests beside each
implementation.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from .._execution import CanonicalValue
from .._interfaces import ArgumentCanonicalizer
from ..exceptions import ArgumentCanonicalizationError
from ..serialization import OpaquePolicy
from ..serialization._utils import encode_canonical

CanonicalizerFactory = Callable[..., ArgumentCanonicalizer]


class Opaque:
    """A value no canonicalizer can be expected to represent."""

    def __init__(self, marker: str = "x") -> None:
        self.marker = marker


@dataclasses.dataclass(frozen=True)
class Holder:
    """Somewhere for an opaque value to hide."""

    inner: Opaque


class ByMarker(OpaquePolicy):
    def represent(self, value: object) -> CanonicalValue:
        return getattr(value, "marker", None)


class ArgumentCanonicalizerContract:
    """Conformance suite for an `ArgumentCanonicalizer` implementation.

    Subclass it and supply a ``canonicalizer_factory`` fixture returning a
    callable that builds one, optionally with an ``opaque_policy``.
    """

    @pytest.fixture
    def canonicalizer_factory(self) -> CanonicalizerFactory:
        raise NotImplementedError

    @pytest.fixture
    def canonicalizer(
        self, canonicalizer_factory: CanonicalizerFactory
    ) -> ArgumentCanonicalizer:
        return canonicalizer_factory()

    def test_a_canonical_form_is_in_the_json_data_model(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        # The encoder is what turns the form into an execution's key, so a form
        # it cannot take is not a canonical form.
        encode_canonical(canonicalizer.canonicalize({"a": 1, "b": "two", "c": None}))

    def test_every_bound_name_reaches_the_canonical_form(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        # A name that vanished would let two different calls share one key.
        assert canonicalizer.canonicalize(
            {"a": 1, "b": 2}
        ) != canonicalizer.canonicalize({"a": 1})

    def test_the_same_arguments_canonicalize_the_same_way(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        assert canonicalizer.canonicalize(
            {"a": 42, "b": "hello"}
        ) == canonicalizer.canonicalize({"a": 42, "b": "hello"})

    def test_different_values_under_one_name_differ(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        assert canonicalizer.canonicalize({"a": 1}) != canonicalizer.canonicalize(
            {"a": 2}
        )

    def test_no_arguments_canonicalizes(self, canonicalizer: ArgumentCanonicalizer):
        encode_canonical(canonicalizer.canonicalize({}))

    # -- The opaque policy ---------------------------------------------------

    def test_a_value_with_no_representation_is_refused_by_default(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        with pytest.raises(ArgumentCanonicalizationError):
            canonicalizer.canonicalize({"a": Opaque()})

    def test_a_policy_decides_what_an_opaque_value_becomes(
        self, canonicalizer_factory: CanonicalizerFactory
    ):
        canonicalizer = canonicalizer_factory(opaque_policy=ByMarker())

        assert canonicalizer.canonicalize(
            {"a": Opaque("one")}
        ) != canonicalizer.canonicalize({"a": Opaque("two")})
        assert canonicalizer.canonicalize(
            {"a": Opaque("one")}
        ) == canonicalizer.canonicalize({"a": Opaque("one")})

    def test_a_policy_reaches_a_value_nested_inside_another(
        self, canonicalizer_factory: CanonicalizerFactory
    ):
        canonicalizer = canonicalizer_factory(opaque_policy=ByMarker())

        assert canonicalizer.canonicalize(
            {"a": Holder(Opaque("one"))}
        ) != canonicalizer.canonicalize({"a": Holder(Opaque("two"))})
