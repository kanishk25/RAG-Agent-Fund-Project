"""P3 exit criteria, measured against the real embedding model.

Everything else in the retrieval suite uses a fake embedder, because filtering
and reconciliation rules should not depend on a model download. This file is the
opposite on purpose: recall@4 is a claim about `all-MiniLM-L6-v2` specifically,
and a fake embedder measuring it would be measuring nothing.

The three criteria, in the order the plan states them:

  1. Golden-set questions retrieve the correct chunk in the top 4 (recall@4)
  2. Cross-scheme leakage never happens — the single most important test here
  3. Unresolvable-scheme queries return empty rather than guessing

Criterion 2 is the one ARCH §6.1 calls the dominant failure mode of the pure-
vector choice, and it is tested twice over: once structurally in
`test_search.py` against the fake embedder, and once here against real
embeddings, because a filter that works on toy vectors and fails on real ones
would be a filter that works for the wrong reason.

The index is built once per session (~35 embeddings) and reused.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from mf_faq.retrieval.chunk import chunk_cards
from mf_faq.retrieval.search import Searcher, SearchOutcome
from mf_faq.retrieval.store import VectorStore
from mf_faq.settings import get_settings

EVAL_DIR = pathlib.Path(__file__).parent.parent / "eval"
GOLDEN = yaml.safe_load((EVAL_DIR / "golden.yaml").read_text(encoding="utf-8"))["cases"]
AMBIGUITY = yaml.safe_load((EVAL_DIR / "ambiguity.yaml").read_text(encoding="utf-8"))["cases"]
REFUSAL = yaml.safe_load((EVAL_DIR / "refusal.yaml").read_text(encoding="utf-8"))["cases"]


@pytest.fixture(scope="session")
def real_embedder():
    """The configured model. Skips rather than fails where it cannot be loaded."""
    from mf_faq.retrieval.embedder import build_embedder

    embedder = build_embedder()
    try:
        embedder.encode_query("warm up")
    except Exception as exc:  # pragma: no cover - offline CI without the HF cache
        pytest.skip(f"embedding model unavailable: {type(exc).__name__}: {exc}")
    return embedder


@pytest.fixture(scope="session")
def real_index(tmp_path_factory, real_embedder):
    """All 35 real fact cards, embedded with the real model. Built once."""
    from mf_faq.ingest.fact_card import render_cards
    from mf_faq.ingest.parse import parse_facts
    from mf_faq.settings import get_sources
    from tests.conftest import groww_payload

    cards = []
    for scheme in get_sources().schemes:
        facts, _ = parse_facts(
            groww_payload(scheme.scheme_id),
            scheme_id=scheme.scheme_id,
            source_url=str(scheme.url),
            doc_types=scheme.extract,
        )
        cards.extend(render_cards(facts, scheme))

    store = VectorStore(
        path=tmp_path_factory.mktemp("chroma_real"),
        collection_name="mf_facts_eval",
        embedder=real_embedder,
    )
    store.upsert(chunk_cards(cards))
    assert store.count() == 35
    return store


@pytest.fixture(scope="session")
def searcher(real_index) -> Searcher:
    """The configured searcher — floor included, as a user would hit it."""
    return Searcher(real_index)


@pytest.fixture(scope="session")
def ranker(real_index) -> Searcher:
    """Floor disabled, to measure *ranking* on its own.

    recall@4 is a question about ordering; the floor is a separate policy about
    confidence. Measuring them through one number would let a floor change look
    like a retrieval regression, and vice versa.
    """
    return Searcher(real_index, similarity_floor=-1.0)


@pytest.fixture(scope="session")
def golden_results(ranker) -> list[tuple[dict, object]]:
    """Every golden question ranked once, reused across the measurements."""
    return [(case, ranker.search(case["question"])) for case in GOLDEN]


@pytest.fixture(scope="session")
def refusal_results(ranker) -> list[tuple[dict, object]]:
    """The refusal set ranked the same way — the floor's negative evidence."""
    return [(case, ranker.search(case["question"])) for case in REFUSAL]


class TestRecallAtFour:
    """Exit criterion 1."""

    @pytest.mark.parametrize(
        "case", [c for c in GOLDEN if c["id"] != "G-multi-elss-identify"], ids=lambda c: c["id"]
    )
    def test_the_expected_chunk_is_retrieved(self, ranker, case):
        result = ranker.search(case["question"])
        assert result.ok, f"{case['id']}: {result.outcome.value}"
        retrieved = [(c.scheme_id, c.doc_type) for c in result.chunks]
        assert (case["scheme_id"], case["doc_type"]) in retrieved, (
            f"{case['id']} missed its chunk. Got {retrieved}"
        )

    def test_recall_at_4_is_measured_and_only_the_known_case_misses(self, golden_results):
        """The measurement, with its one accepted miss named rather than hidden.

        `G-multi-elss-identify` asks which fund is an ELSS fund and is tagged
        `doc_type: lock_in`. Its answer comes from the scheme's identity, not
        from any one of that scheme's seven facts, so no ranking of them puts
        `lock_in` on top. It is left failing on purpose: the eval sets were
        authored before this code so they would not be reshaped to fit it.
        """
        misses = [
            case["id"]
            for case, result in golden_results
            if not result.ok
            or (case["scheme_id"], case["doc_type"])
            not in [(c.scheme_id, c.doc_type) for c in result.chunks]
        ]
        recall = 1 - len(misses) / len(golden_results)
        print(f"\nrecall@4 = {recall:.1%} over {len(golden_results)} golden cases")
        assert misses == ["G-multi-elss-identify"], f"recall@4 = {recall:.1%}; missed {misses}"
        assert recall >= 0.97

    def test_recall_at_1_is_reported(self, golden_results):
        """Not a gate — a number worth knowing before P5 tunes anything.

        Top-1 is what the model would answer from if `top_k` were 1. It is
        allowed to be below 100%; `top_k=4` exists precisely because it is.
        """
        hits = sum(
            1
            for case, result in golden_results
            if result.ok
            and (result.chunks[0].scheme_id, result.chunks[0].doc_type)
            == (case["scheme_id"], case["doc_type"])
        )
        recall_at_1 = hits / len(golden_results)
        print(f"\nrecall@1 = {recall_at_1:.1%}")
        # Measured at 97.1% after the scheme name was removed from the vectors
        # (`chunk.py`), up from 82.4% before. Asserted loosely as a regression
        # guard — the exact figure will move as the corpus does.
        assert recall_at_1 >= 0.90


class TestCrossSchemeLeakage:
    """Exit criterion 2 — the most important test in Phase 3."""

    @pytest.mark.parametrize("case", GOLDEN, ids=lambda c: c["id"])
    def test_no_chunk_from_another_scheme(self, searcher, case):
        result = searcher.search(case["question"])
        foreign = [c.doc_id for c in result.chunks if c.scheme_id != case["scheme_id"]]
        assert not foreign, f"{case['id']} leaked {foreign}"

    def test_the_corpus_makes_leakage_possible_in_principle(self, real_index):
        """Guards the test above from passing vacuously.

        Every scheme holds the same seven doc_types, so a query naming one fund
        has four wrong-fund chunks of the identical fact competing for the same
        top-4. If that ever stopped being true, the leakage test would prove
        nothing and this assertion is what would say so.
        """
        state = real_index.indexed_state()
        by_type: dict[str, set[str]] = {}
        for meta in state.values():
            by_type.setdefault(meta["doc_type"], set()).add(meta["scheme_id"])
        assert all(len(schemes) == 5 for schemes in by_type.values())

    def test_without_the_filter_other_schemes_do_surface(self, real_index, real_embedder):
        """The filter is load-bearing, not decorative.

        The same query with `scheme_id=None` pulls in other funds' chunks — so
        the clean results above are the filter working, not the embedding model
        happening to be unambiguous.
        """
        vec = real_embedder.encode_query("What is the expense ratio of the ELSS fund?")
        unfiltered = real_index.query(vec, scheme_id=None, top_k=4)
        assert len({c.scheme_id for c in unfiltered}) > 1


class TestUnresolvableQueriesReturnEmpty:
    """Exit criterion 3."""

    @pytest.mark.parametrize(
        "case", [c for c in AMBIGUITY if c["expect"].get("clarify")], ids=lambda c: c["id"]
    )
    def test_ambiguous_and_unnamed_queries_retrieve_nothing(self, searcher, case):
        result = searcher.search(case["question"])
        assert result.chunks == []
        assert result.outcome in {
            SearchOutcome.SCHEME_AMBIGUOUS,
            SearchOutcome.SCHEME_UNRESOLVED,
        }

    def test_out_of_corpus_funds_retrieve_nothing(self, searcher):
        for query in ["NAV of HDFC Top 100 Fund", "SBI Bluechip Fund expense ratio"]:
            assert searcher.search(query).chunks == []


class TestTheSimilarityFloor:
    """⚠️ The floor cannot tell answerable from refusable. Measured, not assumed.

    The intuition behind a similarity floor is that a question the corpus cannot
    answer will retrieve weakly. Measured against the authored eval sets, that
    is simply false here:

        golden (should answer)  top-1 similarity  0.18 – 0.83
        refusal (must refuse)   top-1 similarity  0.10 – 0.65

    The ranges overlap almost completely, and several refusal cases outscore
    legitimate golden ones — "what is the NAV, and should I buy?" scores 0.57
    because half of it *is* a NAV question, while "how much does motilal oswal
    large and midcap charge in fees" scores 0.30 because casual phrasing shares
    few words with a fact card.

    **So the floor is not the refusal mechanism and must never be tuned as if it
    were.** Refusal is the deterministic intent gate (P4.2), the freshness gate
    (P4.3), the model's own `is_answerable`, and the validator (P4.6) — all of
    which read meaning rather than distance. The floor's only defensible job is
    R-01: catching a query that resembles nothing in the scheme at all.

    These tests therefore record the measurement and hand it to P5.5; they do
    not pick a number. Changing a compliance-relevant threshold belongs to the
    phase that can see answer quality alongside it.
    """

    def test_the_configured_floor_over_refuses_one_golden_case(self, searcher):
        """Recorded as a P5.5 input, not tuned away here.

        `G-phrasing-ter-casual` retrieves the correct expense_ratio chunk at
        **rank 1** and is still refused, because its similarity (0.300) sits
        under the configured 0.35. That is the floor rejecting a good retrieval
        — the over-refusal P5 must weigh against whatever the floor buys.
        """
        floor = get_settings().similarity_floor
        refused = [c["id"] for c in GOLDEN if not searcher.search(c["question"]).ok]
        assert refused == ["G-multi-elss-identify", "G-phrasing-ter-casual"], (
            f"floor {floor} now refuses {refused} — re-measure before changing it"
        )

    def test_the_positive_and_negative_ranges_overlap(self, golden_results, refusal_results):
        """The finding itself. If this ever fails, the floor became meaningful."""
        positives = [r.best_similarity for _, r in golden_results if r.best_similarity is not None]
        negatives = [r.best_similarity for _, r in refusal_results if r.best_similarity is not None]
        assert negatives, "no refusal case resolved a scheme — the sets changed"
        print(
            f"\ngolden  top-1: {min(positives):.3f} – {max(positives):.3f}"
            f"\nrefusal top-1: {min(negatives):.3f} – {max(negatives):.3f}"
        )
        assert max(negatives) > min(positives), (
            "positives and negatives now separate cleanly — a similarity floor "
            "could carry real weight, and P5.5 should revisit it"
        )

    def test_no_floor_separates_the_two_sets(self, golden_results, refusal_results):
        """Stronger form: there is no threshold that admits all golden and no refusal."""
        positives = sorted(r.best_similarity for _, r in golden_results if r.best_similarity)
        negatives = sorted(r.best_similarity for _, r in refusal_results if r.best_similarity)
        assert positives[0] < negatives[-1]

    def test_a_floor_still_catches_a_query_resembling_nothing(self, real_index):
        """R-01 is the floor's real job, and it does that much."""
        searcher = Searcher(real_index, similarity_floor=0.35)
        result = searcher.search(
            "What is the boiling point of tungsten in the Motilal Oswal ELSS Tax Saver Fund?"
        )
        assert result.outcome is SearchOutcome.BELOW_FLOOR


class TestHoldingsTruncation:
    """The measured limitation recorded in `chunk.py`, pinned so it stays true."""

    def test_holdings_cards_exceed_the_model_window(self, real_embedder, real_index):
        tokenizer = real_embedder._load().tokenizer
        state = real_index.indexed_state()
        holdings = [k for k, m in state.items() if m["doc_type"] == "holdings"]
        assert holdings
        lengths = {
            chunk_id: len(
                tokenizer.encode(real_index.collection.get(ids=[chunk_id])["documents"][0])
            )
            for chunk_id in holdings
        }
        assert max(lengths.values()) > real_embedder.max_seq_length, (
            "holdings now fit the window — the caveat in chunk.py can be removed"
        )

    def test_holdings_are_still_retrievable_despite_truncation(self, searcher):
        """Truncation eats the tail; the scheme name and 'holdings' lead the card."""
        for case in [c for c in GOLDEN if c["doc_type"] == "holdings"]:
            result = searcher.search(case["question"])
            assert result.ok
            assert "holdings" in [c.doc_type for c in result.chunks], case["id"]

    def test_the_full_holdings_list_is_still_served(self, searcher):
        """Only the embedding is truncated — the served text must be complete."""
        result = searcher.search(
            "What are the top holdings of Motilal Oswal Large and Midcap Fund?"
        )
        chunk = next(c for c in result.chunks if c.doc_type == "holdings")
        assert chunk.text.rstrip().endswith("%.")
        assert chunk.text.count("%") > 20
