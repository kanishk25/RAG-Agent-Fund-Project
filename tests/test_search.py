"""Search outcomes, the scheme filter, and the similarity floor (P3.6, P3.7).

The cross-scheme leakage class is the important one here — ARCH §6.1 calls it
the dominant failure mode of the pure-vector choice, and it is the only failure
in this system that produces a fluent, correctly-cited, correctly-dated answer
about the wrong fund.
"""

from __future__ import annotations

import dataclasses

import pytest

from mf_faq.retrieval.chunk import chunk_cards
from mf_faq.retrieval.search import Searcher, SearchOutcome

FLOOR = 0.35


@pytest.fixture
def searcher(populated_store) -> Searcher:
    return Searcher(populated_store, top_k=4, similarity_floor=FLOOR)


class TestCrossSchemeLeakage:
    """P3 exit criterion: a query naming Scheme A never returns Scheme B chunks."""

    @pytest.mark.parametrize(
        "scheme_id",
        ["mo_large_midcap", "mo_bse_value", "mo_elss", "mo_next50", "mo_bse_fin"],
    )
    def test_every_chunk_belongs_to_the_named_scheme(self, searcher, scheme_id, corpus_cards):
        scheme = next(c for c in corpus_cards if c.scheme_id == scheme_id)
        for doc_type in ["nav", "expense ratio", "exit load", "minimum sip", "holdings"]:
            result = searcher.search(f"{doc_type} of {scheme.text.split(' of ')[-1][:60]}")
            assert all(c.scheme_id == scheme_id for c in result.chunks), (
                f"leaked into {scheme_id}: {[c.doc_id for c in result.chunks]}"
            )

    def test_a_filter_is_always_applied_when_chunks_are_returned(self, searcher):
        """No path may return chunks without having resolved a scheme first."""
        result = searcher.search("What is the NAV of Motilal Oswal ELSS Tax Saver Fund?")
        assert result.ok
        assert result.scheme_id == "mo_elss"
        assert {c.scheme_id for c in result.chunks} == {"mo_elss"}

    def test_the_other_schemes_hold_the_same_fact_and_are_still_excluded(self, searcher):
        """All five funds have an expense_ratio chunk; only one may come back."""
        result = searcher.search("expense ratio of Motilal Oswal Nifty Next 50 Index Fund")
        assert result.ok
        assert {c.scheme_id for c in result.chunks} == {"mo_next50"}


class TestUnresolvableQueriesReturnEmpty:
    """P3 exit criterion: return empty rather than guessing a scheme."""

    @pytest.mark.parametrize(
        "query",
        ["What is the expense ratio?", "And its exit load?", "What's the NAV today?"],
    )
    def test_no_scheme_named(self, searcher, query):
        result = searcher.search(query)
        assert result.outcome is SearchOutcome.SCHEME_UNRESOLVED
        assert result.chunks == []

    def test_ambiguous_scheme_returns_empty_with_candidates(self, searcher):
        result = searcher.search("expense ratio of the Motilal Oswal BSE index fund")
        assert result.outcome is SearchOutcome.SCHEME_AMBIGUOUS
        assert result.chunks == []
        assert set(result.candidates) == {"mo_bse_value", "mo_bse_fin"}

    def test_out_of_corpus_fund_returns_empty(self, searcher):
        result = searcher.search("What is the NAV of HDFC Top 100 Fund?")
        assert result.outcome is SearchOutcome.SCHEME_UNRESOLVED
        assert result.chunks == []


class TestOutcomesAreDistinguishable:
    """Every empty result names its reason — P4 says something different for each."""

    def test_empty_index_is_not_a_user_error(self, vector_store):
        """R-03: nothing ingested is an ops fault, not an unknown scheme."""
        result = Searcher(vector_store, similarity_floor=FLOOR).search(
            "NAV of Motilal Oswal ELSS Tax Saver Fund"
        )
        assert result.outcome is SearchOutcome.EMPTY_INDEX

    def test_a_known_scheme_with_no_facts_is_not_an_unknown_scheme(
        self, vector_store, corpus_cards
    ):
        """R-04: the sharp one. 'not available' ≠ 'we do not cover that fund'."""
        without_elss = [c for c in corpus_cards if c.scheme_id != "mo_elss"]
        vector_store.upsert(chunk_cards(without_elss))
        result = Searcher(vector_store, similarity_floor=FLOOR).search(
            "NAV of Motilal Oswal ELSS Tax Saver Fund"
        )
        assert result.outcome is SearchOutcome.NO_FACTS_FOR_SCHEME
        assert result.scheme_id == "mo_elss"  # the scheme *did* resolve
        assert result.chunks == []

    def test_all_six_outcomes_are_reachable(self):
        assert len(set(SearchOutcome)) == 6


class TestSimilarityFloor:
    """R-01: below the floor returns no chunks, never weak ones."""

    def test_an_impossible_floor_returns_nothing(self, populated_store):
        searcher = Searcher(populated_store, similarity_floor=1.1)
        result = searcher.search("NAV of Motilal Oswal ELSS Tax Saver Fund")
        assert result.outcome is SearchOutcome.BELOW_FLOOR
        assert result.chunks == []

    def test_weak_hits_are_reported_but_not_returned(self, populated_store):
        searcher = Searcher(populated_store, similarity_floor=1.1)
        result = searcher.search("NAV of Motilal Oswal ELSS Tax Saver Fund")
        assert result.discarded  # they existed
        assert result.chunks == []  # they did not escape

    def test_the_boundary_is_inclusive(self, populated_store, corpus_cards, fake_embedder):
        """R-02, pinned. A score exactly at the floor passes.

        The query is a chunk's own text, which scores exactly 1.0, so a floor of
        1.0 tests the boundary itself rather than an approximation of it.
        """
        card = next(c for c in corpus_cards if c.doc_id == "mo_elss:nav")
        result = Searcher(populated_store, similarity_floor=1.0).search(card.text)
        assert result.ok
        assert result.chunks[0].similarity == pytest.approx(1.0, abs=1e-6)

    def test_just_above_the_boundary_is_excluded(self, populated_store, corpus_cards):
        card = next(c for c in corpus_cards if c.doc_id == "mo_elss:nav")
        result = Searcher(populated_store, similarity_floor=1.0 + 1e-3).search(card.text)
        assert result.outcome is SearchOutcome.BELOW_FLOOR

    def test_floor_defaults_to_settings(self, populated_store):
        from mf_faq.settings import get_settings

        assert Searcher(populated_store).similarity_floor == get_settings().similarity_floor


class TestTopK:
    def test_returns_at_most_top_k(self, populated_store):
        searcher = Searcher(populated_store, top_k=2, similarity_floor=-1.0)
        result = searcher.search("NAV of Motilal Oswal ELSS Tax Saver Fund")
        assert len(result.chunks) <= 2

    def test_top_k_defaults_to_settings(self, populated_store):
        from mf_faq.settings import get_settings

        assert Searcher(populated_store).top_k == get_settings().top_k

    def test_asking_for_more_than_the_scheme_holds_is_safe(self, populated_store):
        searcher = Searcher(populated_store, top_k=50, similarity_floor=-1.0)
        result = searcher.search("NAV of Motilal Oswal ELSS Tax Saver Fund")
        assert len(result.chunks) == 7  # the scheme's whole fact set, no more


class TestResultShape:
    def test_metadata_survives_to_the_caller(self, searcher):
        """P4 cites source_url and footers source_as_of from exactly these."""
        result = searcher.search("NAV of Motilal Oswal ELSS Tax Saver Fund")
        chunk = result.chunks[0]
        assert chunk.source_url.startswith("https://groww.in/")
        assert chunk.source_as_of
        assert chunk.doc_id and chunk.doc_type and chunk.text

    def test_chunks_are_ordered_by_similarity(self, populated_store):
        searcher = Searcher(populated_store, top_k=4, similarity_floor=-1.0)
        sims = [c.similarity for c in searcher.search("holdings of the ELSS fund").chunks]
        assert sims == sorted(sims, reverse=True)

    def test_best_similarity_reports_discarded_hits_too(self, populated_store):
        searcher = Searcher(populated_store, similarity_floor=1.1)
        result = searcher.search("NAV of Motilal Oswal ELSS Tax Saver Fund")
        assert result.best_similarity is not None

    def test_result_is_frozen(self, searcher):
        result = searcher.search("NAV of Motilal Oswal ELSS Tax Saver Fund")
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.outcome = SearchOutcome.BELOW_FLOOR  # type: ignore[misc]
