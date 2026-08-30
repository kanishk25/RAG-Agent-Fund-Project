"""`ask()` wired to the REAL `retrieval.search.Searcher`, not a hand-rolled fake.

`test_pipeline_generation.py` proves the guardrail chain's branches are
fail-closed using fakes shaped to match `Searcher`'s interface. This file
proves the interface match is not just assumed: it drives `ask()` against the
actual `Searcher` class, over the offline fake-embedder store built in
`conftest.py` (`populated_store`), so a real scheme resolution and a real
Chroma query happen — only the LLM call is faked, because that is the one
piece with no offline equivalent (P5 runs it for real, throttled, against
Groq).
"""

from __future__ import annotations

from mf_faq.generation.answer import FactAnswer, GenerationResult
from mf_faq.generation.pipeline import ask
from mf_faq.retrieval.search import Searcher


class ScriptedAnswerClient:
    """Cites whichever retrieved chunk matches `target_doc_type`, whatever
    position the (crude, hash-based) fake embedder happened to rank it at —
    the pipeline's own citation-index contract, not a hardcoded guess at
    ranking order, is what this is testing.

    If `target_doc_type` is not among the chunks shown, this mimics what a
    real model is relied on to do in exactly that situation (R-05): refuse
    with is_answerable=false, because none of the given context answers the
    question. This is the case where the specific fact asked about was
    dropped by the freshness gate but OTHER, unrelated facts for the same
    scheme survived it — see `test_a_stale_target_among_other_survivors_is_a_model_refusal`.
    """

    def __init__(self, target_doc_type: str, answer: str):
        self.target_doc_type = target_doc_type
        self.answer = answer
        self.calls: list[tuple] = []

    def generate(self, question, chunks):
        self.calls.append((question, chunks))
        index = next(
            (
                i
                for i, item in enumerate(chunks, start=1)
                if item.chunk.doc_type == self.target_doc_type
            ),
            None,
        )
        if index is None:
            fact = FactAnswer(False, "not supported by the given context", "", None)
        else:
            fact = FactAnswer(True, None, self.answer, index)
        return GenerationResult(fact, prompt_tokens=100, completion_tokens=20, total_tokens=120)


class TestRealSearcherThinVerticalSlice:
    """The P4 exit criterion, driven through the actual retrieval stack."""

    def test_one_scheme_one_fact_through_every_real_gate(self, populated_store):
        searcher = Searcher(populated_store, top_k=4, similarity_floor=0.35)
        answer_client = ScriptedAnswerClient(
            target_doc_type="expense_ratio", answer="The expense ratio is 0.97% per annum."
        )

        response = ask(
            "What is the expense ratio of Motilal Oswal ELSS Tax Saver Fund?",
            searcher=searcher,
            answer_client=answer_client,
        )

        assert response.answered is True
        assert "0.97" in response.text
        assert response.citation_url.endswith("most-focused-long-term-fund-direct-growth")
        assert response.source_as_of  # a real date came back from the real store
        assert len(answer_client.calls) == 1

    def test_real_cross_scheme_leakage_check(self, populated_store):
        """The chunks `ask()` actually shows the model must all be one scheme."""
        searcher = Searcher(populated_store, top_k=4, similarity_floor=0.35)
        answer_client = ScriptedAnswerClient(target_doc_type="expense_ratio", answer="x")

        ask(
            "What is the expense ratio of Motilal Oswal ELSS Tax Saver Fund?",
            searcher=searcher,
            answer_client=answer_client,
        )

        _, chunks_shown = answer_client.calls[0]
        assert {c.chunk.scheme_id for c in chunks_shown} == {"mo_elss"}

    def test_a_real_ambiguous_query_never_reaches_generation(self, populated_store):
        searcher = Searcher(populated_store, top_k=4, similarity_floor=0.35)
        answer_client = ScriptedAnswerClient(target_doc_type="expense_ratio", answer="x")

        response = ask(
            "expense ratio of the Motilal Oswal BSE index fund",
            searcher=searcher,
            answer_client=answer_client,
        )

        assert response.answered is False
        assert response.refusal_reason == "ambiguous_scheme"
        assert answer_client.calls == []

    def test_a_real_out_of_corpus_query_never_reaches_generation(self, populated_store):
        searcher = Searcher(populated_store, top_k=4, similarity_floor=0.35)
        answer_client = ScriptedAnswerClient(target_doc_type="expense_ratio", answer="x")

        response = ask(
            "What is the expense ratio of HDFC Flexi Cap Fund?",
            searcher=searcher,
            answer_client=answer_client,
        )

        assert response.answered is False
        assert answer_client.calls == []

    def test_real_nav_freshness_gate_answers_when_current(self, populated_store):
        """The fixture corpus's real NAV date, pinned as 'today' via `now`."""
        from datetime import datetime

        from mf_faq.guardrails.freshness import IST

        searcher = Searcher(populated_store, top_k=4, similarity_floor=0.35)
        answer_client = ScriptedAnswerClient(target_doc_type="nav", answer="x")

        nav_meta = next(
            m
            for m in populated_store.indexed_state().values()
            if m["doc_type"] == "nav" and m["scheme_id"] == "mo_elss"
        )
        nav_date = datetime.fromisoformat(nav_meta["source_as_of"]).replace(tzinfo=IST)

        response = ask(
            "What is the NAV of Motilal Oswal ELSS Tax Saver Fund?",
            searcher=searcher,
            answer_client=answer_client,
            now=nav_date,  # "today" IS the NAV's own declaration date
        )
        assert response.answered is True
        assert response.stale is False

    def test_a_stale_nav_is_dropped_from_the_chunks_shown_to_the_model(self, populated_store):
        """The freshness gate acts per-chunk, not on the retrieval set as a
        whole (ARCH §7.3: "a document", singular). With `top_k=4`, a NAV
        question also retrieves this scheme's other facts (benchmark,
        expense_ratio, exit_load) alongside NAV. Once NAV goes stale, THOSE
        survive — benchmark is exempt, the others flag rather than refuse —
        so generation still runs, just without the one chunk that could
        actually answer "what is the NAV". A real model is relied on to
        notice that and refuse (R-05); this fake mimics exactly that judgment
        rather than crashing when the target type is absent.
        """
        from datetime import datetime, timedelta

        from mf_faq.guardrails.freshness import IST

        searcher = Searcher(populated_store, top_k=4, similarity_floor=0.35)
        answer_client = ScriptedAnswerClient(target_doc_type="nav", answer="x")

        nav_meta = next(
            m
            for m in populated_store.indexed_state().values()
            if m["doc_type"] == "nav" and m["scheme_id"] == "mo_elss"
        )
        nav_date = datetime.fromisoformat(nav_meta["source_as_of"]).replace(tzinfo=IST)
        far_future = nav_date + timedelta(days=30)  # comfortably past 1 business day

        response = ask(
            "What is the NAV of Motilal Oswal ELSS Tax Saver Fund?",
            searcher=searcher,
            answer_client=answer_client,
            now=far_future,
        )
        assert response.answered is False
        assert response.refusal_reason == "not_answerable"  # the model's own refusal
        assert len(answer_client.calls) == 1  # generation DID run — see docstring
        _, chunks_shown = answer_client.calls[0]
        assert "nav" not in {c.chunk.doc_type for c in chunks_shown}  # dropped by freshness

        # `render_stale_refusal` ("stale_content") is the NARROWER case: NAV as
        # the ONLY retrieved chunk, so `kept` goes empty once it is dropped —
        # see test_pipeline_generation.py::TestFreshnessGateRunsBeforeGeneration
        # ::test_a_stale_nav_never_reaches_generation for that exact scenario
        # with a controlled single-chunk fake searcher.
