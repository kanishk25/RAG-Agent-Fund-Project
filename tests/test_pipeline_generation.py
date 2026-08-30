"""The full guardrail chain wired end to end (P4.8).

Uses hand-built fakes for the searcher and answer client so every branch —
each retrieval outcome, each freshness verdict, generation failure, validator
failure — is reached deterministically. This is NOT where golden.yaml /
refusal.yaml are run against a real model; that is P5's job. This file proves
the WIRING is fail-closed at every branch (P4.8's exit criterion), not that
any particular question is answered correctly.
"""

from __future__ import annotations

from mf_faq.generation.answer import FactAnswer, GenerationResult, Unavailable
from mf_faq.generation.pipeline import ask
from mf_faq.retrieval.resolve import Resolution, SchemeMatch
from mf_faq.retrieval.search import SearchOutcome, SearchResult
from mf_faq.retrieval.store import RetrievedChunk
from mf_faq.settings import get_settings, get_sources

SOURCES = get_sources()


def _chunk(**over) -> RetrievedChunk:
    defaults = dict(
        chunk_id="mo_elss:nav#0",
        doc_id="mo_elss:nav",
        scheme_id="mo_elss",
        doc_type="nav",
        text="The latest declared NAV of Motilal Oswal ELSS Tax Saver Fund is 41.70 per unit.",
        source_url="https://groww.in/mutual-funds/motilal-oswal-most-focused-long-term-fund-direct-growth",
        source_as_of="2026-08-28",
        similarity=0.9,
    )
    return RetrievedChunk(**{**defaults, **over})


class FakeSearcher:
    """Returns one canned `SearchResult` regardless of query text."""

    def __init__(self, result: SearchResult):
        self.result = result
        self.queries: list[str] = []

    def search(self, query: str) -> SearchResult:
        self.queries.append(query)
        return self.result


def _match(scheme_id: str = "mo_elss") -> SchemeMatch:
    return SchemeMatch(Resolution.RESOLVED, scheme_id, (scheme_id,), (), "test")


def _ok_result(chunks: list[RetrievedChunk], scheme_id: str = "mo_elss") -> SearchResult:
    return SearchResult(
        outcome=SearchOutcome.OK, chunks=chunks, scheme=_match(scheme_id), floor=0.35
    )


class FakeAnswerClient:
    """Returns one canned `GenerationResult`, or raises a canned exception."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[tuple] = []

    def generate(self, question, chunks):
        self.calls.append((question, chunks))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _must_not_be_called():
    class _Refuser:
        def generate(self, *a, **k):
            raise AssertionError("answer_client.generate() must not be called on this path")

    return _Refuser()


def _generation(is_answerable=True, citation_index=1, answer="The NAV is 41.70.", **usage):
    fact = FactAnswer(is_answerable, None if is_answerable else "reason", answer, citation_index)
    return GenerationResult(
        answer=fact,
        prompt_tokens=usage.get("prompt_tokens", 100),
        completion_tokens=usage.get("completion_tokens", 20),
        total_tokens=usage.get("total_tokens", 120),
    )


NAV_CHUNK = _chunk(
    text="The latest declared NAV of Motilal Oswal ELSS Tax Saver Fund is 41.70 per unit."
)


class TestThinVerticalSlice:
    """The P4 exit criterion: one scheme, one fact, through every gate."""

    def test_a_clean_nav_question_is_answered_end_to_end(self):
        searcher = FakeSearcher(_ok_result([NAV_CHUNK]))
        answer_client = FakeAnswerClient(_generation(answer="The NAV is 41.70."))

        response = ask(
            "What is the NAV of Motilal Oswal ELSS Tax Saver Fund?",
            searcher=searcher,
            answer_client=answer_client,
        )

        assert response.answered is True
        assert "41.70" in response.text
        assert response.source_as_of == "2026-08-28"
        assert response.citation_url == NAV_CHUNK.source_url
        assert response.disclaimer == "Facts-only. No investment advice."


class TestInputValidation:
    def test_empty_query(self):
        response = ask("")
        assert response.answered is False
        assert response.refusal_reason == "empty_query"

    def test_whitespace_only_query(self):
        response = ask("     ")
        assert response.answered is False
        assert response.refusal_reason == "empty_query"

    def test_oversized_query_is_rejected_before_any_llm_call(self):
        """Q-13 — never spends a token."""
        settings = get_settings()
        huge = "a" * (settings.max_query_chars + 1)
        response = ask(
            huge,
            searcher=FakeSearcher(_ok_result([NAV_CHUNK])),
            answer_client=_must_not_be_called(),
        )
        assert response.answered is False
        assert response.refusal_reason == "query_too_long"


class TestPiiGateRunsFirst:
    def test_pii_query_never_reaches_the_searcher(self):
        searcher = FakeSearcher(_ok_result([NAV_CHUNK]))
        response = ask(
            "My PAN is ABCDE1234F, what is the NAV?",
            searcher=searcher,
            answer_client=_must_not_be_called(),
        )
        assert response.answered is False
        assert response.refusal_reason == "pii"
        assert searcher.queries == []  # never even resolved a scheme

    def test_pii_never_appears_in_a_log_record(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="mf_faq.generation.pipeline"):
            ask("My PAN is ABCDE1234F, what is the NAV?", answer_client=_must_not_be_called())
        for record in caplog.records:
            assert "ABCDE1234F" not in record.getMessage()
            assert "ABCDE1234F" not in str(record.__dict__)


class TestDeterministicIntentRefusals:
    def test_advisory_query_never_reaches_retrieval(self):
        searcher = FakeSearcher(_ok_result([NAV_CHUNK]))
        response = ask(
            "Should I invest in Motilal Oswal Large and Midcap Fund?",
            searcher=searcher,
            answer_client=_must_not_be_called(),
        )
        assert response.answered is False
        assert response.refusal_reason == "advisory_direct"
        assert searcher.queries == []

    def test_performance_query_routes_to_the_resolved_schemes_factsheet(self):
        response = ask(
            "What were the 1-year returns of Motilal Oswal Large and Midcap Fund?",
            answer_client=_must_not_be_called(),
        )
        assert response.refusal_reason == "performance_barred"
        assert response.link is not None
        assert "large-and-midcap" in str(response.link.url)

    def test_mixed_query_carries_no_value_structurally(self):
        """Q-06 — never even resolves a scheme, let alone fetches a fact."""
        searcher = FakeSearcher(_ok_result([NAV_CHUNK]))
        response = ask(
            "What is the NAV of Motilal Oswal ELSS Tax Saver Fund, and should I buy it?",
            searcher=searcher,
            answer_client=_must_not_be_called(),
        )
        assert response.refusal_reason == "mixed_factual_advisory"
        assert "41.70" not in response.text
        assert searcher.queries == []


class TestRetrievalOutcomes:
    def test_empty_index_is_service_unavailable_not_a_refusal(self):
        result = SearchResult(outcome=SearchOutcome.EMPTY_INDEX, floor=0.35)
        response = ask("NAV of Motilal Oswal ELSS Tax Saver Fund", searcher=FakeSearcher(result))
        assert response.answered is False
        assert response.refusal_reason == "service_unavailable"
        assert response.link is None

    def test_ambiguous_scheme_lists_candidates(self):
        match = SchemeMatch(
            Resolution.AMBIGUOUS, None, ("mo_bse_value", "mo_bse_fin"), ("bse",), "test"
        )
        result = SearchResult(outcome=SearchOutcome.SCHEME_AMBIGUOUS, scheme=match, floor=0.35)
        response = ask("BSE fund expense ratio", searcher=FakeSearcher(result))
        assert response.answered is False
        assert response.refusal_reason == "ambiguous_scheme"
        assert "BSE Enhanced Value" in response.text
        assert "BSE Financials ex Bank 30" in response.text

    def test_unresolved_scheme_lists_all_five_covered_schemes(self):
        match = SchemeMatch(Resolution.NO_SCHEME, None, (), (), "test")
        result = SearchResult(outcome=SearchOutcome.SCHEME_UNRESOLVED, scheme=match, floor=0.35)
        response = ask("What is the expense ratio?", searcher=FakeSearcher(result))
        assert response.refusal_reason == "scheme_not_covered"
        for scheme in SOURCES.schemes:
            assert scheme.display_name in response.text

    def test_no_facts_for_scheme_names_the_scheme_not_unknown(self):
        """R-04, exercised in isolation from the deterministic intent filter —
        the query text here must not itself trip a fact_not_covered pattern."""
        result = SearchResult(
            outcome=SearchOutcome.NO_FACTS_FOR_SCHEME, scheme=_match(), floor=0.35
        )
        response = ask(
            "Some unusual detail about Motilal Oswal ELSS Tax Saver Fund",
            searcher=FakeSearcher(result),
        )
        assert response.refusal_reason == "fact_not_covered"
        assert "ELSS Tax Saver" in response.text

    def test_below_floor_refuses_without_a_value(self):
        result = SearchResult(outcome=SearchOutcome.BELOW_FLOOR, scheme=_match(), floor=0.35)
        response = ask("something odd about the ELSS fund", searcher=FakeSearcher(result))
        assert response.answered is False


class TestFreshnessGateRunsBeforeGeneration:
    def test_a_stale_nav_never_reaches_generation(self):
        """nav's max_age is 1 business day, refuse — a week-old NAV must be dropped."""
        stale_nav = _chunk(source_as_of="2026-08-01")  # long past 1 business day
        result = _ok_result([stale_nav])
        response = ask(
            "NAV of Motilal Oswal ELSS Tax Saver Fund",
            searcher=FakeSearcher(result),
            answer_client=_must_not_be_called(),
        )
        assert response.answered is False
        assert response.refusal_reason == "stale_content"

    def test_a_flagged_but_not_refused_chunk_still_reaches_generation(self):
        """expense_ratio flags past 45 days but is never dropped outright."""
        old_ter = _chunk(
            doc_type="expense_ratio",
            text="The expense ratio of the fund is 0.97%.",
            source_as_of="2026-01-01",
        )
        answer_client = FakeAnswerClient(_generation(answer="The expense ratio is 0.97%."))
        response = ask(
            "expense ratio of Motilal Oswal ELSS Tax Saver Fund",
            searcher=FakeSearcher(_ok_result([old_ter])),
            answer_client=answer_client,
        )
        assert response.answered is True
        assert response.stale is True
        assert len(answer_client.calls) == 1

    def test_only_surviving_chunks_are_passed_to_generation(self):
        fresh = _chunk(
            doc_id="mo_elss:expense_ratio", doc_type="expense_ratio", source_as_of="2026-08-20"
        )
        stale_nav = _chunk(source_as_of="2026-08-01")
        answer_client = FakeAnswerClient(_generation())
        ask(
            "NAV and expense ratio of Motilal Oswal ELSS Tax Saver Fund",
            searcher=FakeSearcher(_ok_result([stale_nav, fresh])),
            answer_client=answer_client,
        )
        _, chunks_shown = answer_client.calls[0]
        assert len(chunks_shown) == 1
        assert chunks_shown[0].chunk.doc_type == "expense_ratio"

    def test_citation_index_maps_into_the_post_freshness_list(self):
        """kept[0] is the ORIGINAL second chunk once the first was dropped."""
        stale_nav = _chunk(source_as_of="2026-08-01")
        fresh_ter = _chunk(
            doc_id="mo_elss:expense_ratio",
            doc_type="expense_ratio",
            text="The TER is 0.97%.",
            source_as_of="2026-08-20",
            source_url="https://groww.in/mutual-funds/ELSS",
        )
        answer_client = FakeAnswerClient(_generation(citation_index=1, answer="The TER is 0.97%."))
        response = ask(
            "expense ratio of Motilal Oswal ELSS Tax Saver Fund",
            searcher=FakeSearcher(_ok_result([stale_nav, fresh_ter])),
            answer_client=answer_client,
        )
        assert response.answered is True
        assert response.citation_url == "https://groww.in/mutual-funds/ELSS"


class TestGenerationUnavailable:
    def test_a_429_exhausted_or_api_failure_is_service_unavailable(self):
        response = ask(
            "NAV of Motilal Oswal ELSS Tax Saver Fund",
            searcher=FakeSearcher(_ok_result([NAV_CHUNK])),
            answer_client=FakeAnswerClient(Unavailable("rate limited")),
        )
        assert response.answered is False
        assert response.refusal_reason == "service_unavailable"


class TestModelOwnRefusal:
    def test_is_answerable_false_renders_a_model_refusal(self):
        response = ask(
            "I want the cheapest fund — which one has the lowest expense ratio?",
            searcher=FakeSearcher(_ok_result([NAV_CHUNK])),
            answer_client=FakeAnswerClient(
                _generation(is_answerable=False, citation_index=None, answer="")
            ),
        )
        assert response.answered is False
        assert response.refusal_reason == "not_answerable"

    def test_is_answerable_false_wins_even_if_answer_text_is_populated(self):
        """G-03."""
        bad = _generation(is_answerable=False, citation_index=None, answer="should be ignored")
        response = ask(
            "something",
            searcher=FakeSearcher(_ok_result([NAV_CHUNK])),
            answer_client=FakeAnswerClient(bad),
        )
        assert response.answered is False
        assert "should be ignored" not in response.text


class TestValidatorFailureRefuses:
    def test_four_sentences_is_refused_not_retried(self):
        answer_client = FakeAnswerClient(
            _generation(answer="One. Two. Three. Four.", citation_index=1)
        )
        response = ask(
            "NAV of Motilal Oswal ELSS Tax Saver Fund",
            searcher=FakeSearcher(_ok_result([NAV_CHUNK])),
            answer_client=answer_client,
        )
        assert response.answered is False
        assert response.refusal_reason == "not_answerable"
        assert len(answer_client.calls) == 1  # no retry

    def test_an_ungrounded_number_is_refused(self):
        answer_client = FakeAnswerClient(
            _generation(answer="The NAV is 999.99 per unit.", citation_index=1)
        )
        response = ask(
            "NAV of Motilal Oswal ELSS Tax Saver Fund",
            searcher=FakeSearcher(_ok_result([NAV_CHUNK])),
            answer_client=answer_client,
        )
        assert response.answered is False

    def test_a_foreign_citation_index_is_refused(self):
        answer_client = FakeAnswerClient(_generation(citation_index=99))
        response = ask(
            "NAV of Motilal Oswal ELSS Tax Saver Fund",
            searcher=FakeSearcher(_ok_result([NAV_CHUNK])),
            answer_client=answer_client,
        )
        assert response.answered is False


class TestNoAnswerWithoutACitation:
    """The overarching P4 exit criterion, checked at the integration level."""

    def test_every_answered_response_carries_a_citation_url(self):
        answer_client = FakeAnswerClient(_generation(answer="The NAV is 41.70."))
        response = ask(
            "NAV of Motilal Oswal ELSS Tax Saver Fund",
            searcher=FakeSearcher(_ok_result([NAV_CHUNK])),
            answer_client=answer_client,
        )
        assert response.answered is True
        assert response.citation_url is not None
        assert response.citation_url == NAV_CHUNK.source_url
