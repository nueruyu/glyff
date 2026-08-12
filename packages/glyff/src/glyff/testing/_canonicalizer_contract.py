"""pytest conformance contract for :class:`~glyff.ArgumentCanonicalizer`.

Re-exported from :mod:`glyff.testing`. A bound mapping in, an encodable canonical
value out, and the determinism a key needs. Which Python value becomes which
canonical form, and what happens to one with no representation, are each
implementation's own.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from .._interfaces import ArgumentCanonicalizer
from .._canonical_arguments import CanonicalArguments, CanonicalFallback
from ..exceptions import ArgumentCanonicalizationError

CanonicalizerFactory = Callable[[], ArgumentCanonicalizer]


class ArgumentCanonicalizerContract:
    """Conformance suite for an `ArgumentCanonicalizer` implementation.

    Subclass it and supply a ``canonicalizer_factory`` fixture, which builds an
    equivalent canonicalizer each time it is called.
    """

    @pytest.fixture
    def canonicalizer_factory(self) -> CanonicalizerFactory:
        raise NotImplementedError

    @pytest.fixture
    def canonicalizer(
        self, canonicalizer_factory: CanonicalizerFactory
    ) -> ArgumentCanonicalizer:
        return canonicalizer_factory()

    def test_a_canonical_form_encodes_into_a_key(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        assert canonicalizer.canonicalize({"a": 1, "b": "two", "c": None}).data

    def test_the_bound_name_is_part_of_the_form(
        self, canonicalizer: ArgumentCanonicalizer
    ):
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
        assert canonicalizer.canonicalize({}).data

    def test_a_canonical_form_canonicalizes_to_itself(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        canonical = canonicalizer.canonicalize(
            {"a": 1, "b": ["x", None], "c": {"d": True}, "e": "text"}
        )
        assert canonicalizer.canonicalize(canonical.recorded()) == canonical

    def test_an_opaque_value_canonicalizes_to_the_marker(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        assert canonicalizer.canonicalize(
            {"a": CanonicalFallback("com.example.Service")}
        ) == CanonicalArguments({"a": CanonicalFallback("com.example.Service")})

    def test_a_value_claiming_the_marker_is_refused(
        self, canonicalizer: ArgumentCanonicalizer
    ):
        with pytest.raises(ArgumentCanonicalizationError):
            canonicalizer.canonicalize(
                {"a": {"__glyff_opaque__": "com.example.Service"}}
            )

    def test_a_later_instance_agrees_on_the_form(
        self, canonicalizer_factory: CanonicalizerFactory
    ):
        arguments = {"a": 1, "b": "two"}

        assert canonicalizer_factory().canonicalize(arguments) == (
            canonicalizer_factory().canonicalize(arguments)
        )
