"""pytest conformance contract for :class:`~glyff.ArgumentCanonicalizer`.

Re-exported from :mod:`glyff.testing`. A bound mapping in, an encodable canonical
value out, and the determinism a key needs. Which Python value becomes which
canonical form, and what happens to one with no representation, are each
implementation's own.
"""

from __future__ import annotations

import pytest

from .._interfaces import ArgumentCanonicalizer
from ..serialization._utils import encode_canonical


class ArgumentCanonicalizerContract:
    """Conformance suite for an `ArgumentCanonicalizer` implementation.

    Subclass it and supply a ``canonicalizer`` fixture.
    """

    @pytest.fixture
    def canonicalizer(self) -> ArgumentCanonicalizer:
        raise NotImplementedError

    def test_a_canonical_form_is_in_the_json_data_model(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        # The encoder is what turns the form into an execution's key, so a form
        # it cannot take is not a canonical form.
        encode_canonical(canonicalizer.canonicalize({"a": 1, "b": "two", "c": None}))

    def test_every_bound_name_reaches_the_canonical_form(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        # Same values, different names: a canonicalizer that carried only the
        # values would key `f(a=1)` and `f(b=1)` the same way.
        assert canonicalizer.canonicalize({"a": 1}) != canonicalizer.canonicalize(
            {"b": 1}
        )
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
