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

    def test_a_canonical_form_encodes_into_a_key(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        form = canonicalizer.canonicalize({"a": 1, "b": "two", "c": None})

        assert encode_canonical(form)

    def test_the_bound_name_is_part_of_the_form(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        # One value, two names: a canonicalizer carrying only the values would
        # key `f(a=1)` and `f(b=1)` the same way.
        assert canonicalizer.canonicalize({"a": 1}) != canonicalizer.canonicalize(
            {"b": 1}
        )

    def test_an_extra_bound_argument_changes_the_form(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        assert canonicalizer.canonicalize({"a": 1}) != canonicalizer.canonicalize(
            {"a": 1, "b": 2}
        )

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

    def test_no_arguments_still_encodes_into_a_key(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        form = canonicalizer.canonicalize({})

        assert encode_canonical(form)
