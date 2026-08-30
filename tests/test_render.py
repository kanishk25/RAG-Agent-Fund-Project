"""Refusal and answer rendering (P4.7, P4.9)."""

from __future__ import annotations

import pytest

from mf_faq.generation.render import (
    DISCLAIMER,
    render_ambiguous_refusal,
    render_answer,
    render_below_floor_refusal,
    render_intent_refusal,
    render_model_refusal,
    render_no_facts_refusal,
    render_pii_refusal,
    render_service_unavailable,
    render_stale_refusal,
    render_unresolved_scheme_refusal,
    render_validation_failure_refusal,
)
from mf_faq.guardrails.intent import Reason
from mf_faq.guardrails.validator import ValidatedAnswer, ValidationFailure
from mf_faq.retrieval.store import RetrievedChunk
from mf_faq.settings import get_sources

SOURCES = get_sources()


def _chunk(**over) -> RetrievedChunk:
    defaults = dict(
        chunk_id="mo_elss:nav#0",
        doc_id="mo_elss:nav",
        scheme_id="mo_elss",
        doc_type="nav",
        text="The latest declared NAV of Motilal Oswal ELSS Tax Saver Fund is ₹41.70 per unit.",
        source_url="https://groww.in/mutual-funds/x",
        source_as_of="2026-08-28",
        similarity=0.9,
    )
    return RetrievedChunk(**{**defaults, **over})


class TestRenderAnswer:
    def test_footer_comes_from_the_cited_chunk_not_the_model(self):
        """G-12 — there is nowhere else the date could come from."""
        validated = ValidatedAnswer(text="The NAV is ₹41.70.", cited_chunk=_chunk())
        response = render_answer(validated)
        assert response.answered is True
        assert response.source_as_of == "2026-08-28"
        assert response.citation_url == "https://groww.in/mutual-funds/x"

    def test_the_answer_text_is_carried_through(self):
        validated = ValidatedAnswer(text="The NAV is ₹41.70.", cited_chunk=_chunk())
        assert "₹41.70" in render_answer(validated).text

    def test_stale_flag_adds_a_visible_note(self):
        validated = ValidatedAnswer(text="x", cited_chunk=_chunk())
        response = render_answer(validated, stale=True)
        assert response.stale is True
        assert "older than usual" in response.text.lower()

    def test_no_stale_note_when_fresh(self):
        validated = ValidatedAnswer(text="x", cited_chunk=_chunk())
        response = render_answer(validated, stale=False)
        assert "older than usual" not in response.text.lower()

    def test_disclaimer_is_always_present(self):
        validated = ValidatedAnswer(text="x", cited_chunk=_chunk())
        assert (
            render_answer(validated).disclaimer == DISCLAIMER == "Facts-only. No investment advice."
        )


class TestPiiRefusal:
    def test_no_outbound_link(self):
        response = render_pii_refusal()
        assert response.answered is False
        assert response.link is None
        assert response.refusal_reason == "pii"

    def test_never_echoes_any_user_text(self):
        """The renderer takes no arguments — there is nothing to echo."""
        import inspect

        assert inspect.signature(render_pii_refusal).parameters == {}


class TestAmbiguousRefusal:
    def test_lists_every_candidate_and_picks_none(self):
        candidates = [SOURCES.scheme("mo_bse_value"), SOURCES.scheme("mo_bse_fin")]
        response = render_ambiguous_refusal(candidates)
        assert "BSE Enhanced Value" in response.text
        assert "BSE Financials ex Bank 30" in response.text
        assert response.refusal_reason == "ambiguous_scheme"

    def test_no_outbound_link_for_ambiguity(self):
        """config/refusal_links.yaml: ambiguous_scheme routes to none."""
        candidates = [SOURCES.scheme("mo_bse_value"), SOURCES.scheme("mo_bse_fin")]
        assert render_ambiguous_refusal(candidates).link is None


class TestUnresolvedSchemeRefusal:
    def test_lists_all_five_covered_schemes(self):
        response = render_unresolved_scheme_refusal(SOURCES.schemes)
        for scheme in SOURCES.schemes:
            assert scheme.display_name in response.text

    def test_carries_an_educational_link(self):
        response = render_unresolved_scheme_refusal(SOURCES.schemes)
        assert response.link is not None
        assert "investor-education" in str(response.link.url)


class TestNoFactsRefusal:
    def test_names_the_scheme_not_unknown_fund(self):
        """R-04: 'not available', never 'scheme unknown' — a different message."""
        scheme = SOURCES.scheme("mo_elss")
        response = render_no_facts_refusal(scheme)
        assert scheme.display_name in response.text
        assert "don't have" in response.text.lower()


class TestBelowFloorRefusal:
    def test_renders_without_a_scheme(self):
        response = render_below_floor_refusal(None)
        assert response.answered is False

    def test_renders_with_a_scheme_for_a_factsheet_style_link(self):
        scheme = SOURCES.scheme("mo_elss")
        response = render_below_floor_refusal(scheme)
        assert response.answered is False


class TestIntentRefusals:
    """Every deterministic intent.Reason must have a rendering and the
    correct link category, per config/refusal_links.yaml."""

    @pytest.mark.parametrize(
        ("reason", "expect_link_kind"),
        [
            (Reason.ADVISORY_DIRECT, "educational"),
            (Reason.MIXED_FACTUAL_ADVISORY, "educational"),
            (Reason.INJECTION, "educational"),
            (Reason.BARRED_FIELD, "educational"),
            (Reason.FACT_NOT_COVERED, "educational"),
            (Reason.PLAN_NOT_COVERED, "educational"),
        ],
    )
    def test_educational_reasons_link_to_investor_education(self, reason, expect_link_kind):
        response = render_intent_refusal(reason)
        assert response.link is not None
        assert "investor-education" in str(response.link.url)

    def test_performance_barred_links_to_the_named_schemes_factsheet(self):
        response = render_intent_refusal(Reason.PERFORMANCE_BARRED, scheme_id="mo_large_midcap")
        assert response.link is not None
        assert "motilaloswalmf.com/mutual-funds/motilal-oswal-large-and-midcap" in str(
            response.link.url
        )

    def test_performance_barred_with_no_scheme_uses_the_fallback(self):
        response = render_intent_refusal(Reason.PERFORMANCE_BARRED, scheme_id=None)
        assert response.link is not None
        assert str(response.link.url) == "https://www.motilaloswalmf.com/mutual-funds"

    def test_advisory_never_gets_a_factsheet_link(self):
        """The routing guard R-route-advisory-not-product depends on."""
        response = render_intent_refusal(Reason.ADVISORY_DIRECT, scheme_id="mo_next50")
        assert response.link is not None
        assert "mutual-funds/motilal-oswal" not in str(response.link.url)

    def test_every_reason_has_distinct_wording(self):
        texts = {r: render_intent_refusal(r).text for r in Reason}
        assert len(set(texts.values())) == len(texts)


class TestModelRefusal:
    def test_routes_to_not_answerable(self):
        response = render_model_refusal()
        assert response.refusal_reason == "not_answerable"
        assert response.link is not None


class TestValidationFailureRefusal:
    def test_reads_the_same_as_a_model_refusal_to_the_user(self):
        failure = ValidationFailure(check="sentence_count", detail="4 sentences")
        response = render_validation_failure_refusal(failure)
        assert response.answered is False
        assert response.refusal_reason == "not_answerable"

    def test_the_specific_check_never_appears_in_user_text(self):
        failure = ValidationFailure(check="sentence_count", detail="4 sentences")
        response = render_validation_failure_refusal(failure)
        assert "sentence_count" not in response.text


class TestStaleRefusal:
    def test_routes_to_factsheet(self):
        scheme = SOURCES.scheme("mo_large_midcap")
        response = render_stale_refusal(scheme, "nav")
        assert response.refusal_reason == "stale_content"
        assert response.link is not None
        assert "motilaloswalmf.com/mutual-funds" in str(response.link.url)

    def test_names_the_scheme(self):
        scheme = SOURCES.scheme("mo_large_midcap")
        response = render_stale_refusal(scheme, "nav")
        assert scheme.display_name in response.text


class TestServiceUnavailable:
    def test_no_link_and_no_reason_routing(self):
        """An outage is not a compliance decision — see the module docstring."""
        response = render_service_unavailable()
        assert response.link is None
        assert response.refusal_reason == "service_unavailable"
        assert "temporarily unavailable" in response.text.lower()


class TestNoRefusalEverCarriesAValue:
    """Q-06's must_not_contain_value, structurally: none of these functions
    can even accept a fact value as an argument."""

    def test_no_refusal_renderer_takes_a_retrieved_chunk(self):
        import inspect

        from mf_faq.generation import render as render_module

        for name in dir(render_module):
            if name.startswith("render_") and name not in {"render_answer"}:
                fn = getattr(render_module, name)
                params = inspect.signature(fn).parameters
                for param in params.values():
                    annotation = str(param.annotation)
                    assert "RetrievedChunk" not in annotation, f"{name} accepts a chunk"
