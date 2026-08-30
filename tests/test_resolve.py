"""Scheme resolution (P3.5) — and the refusal to guess.

The exit criterion this file exists for is "unresolvable-scheme queries return
empty rather than guessing a scheme". Every `NO_SCHEME`/`AMBIGUOUS` assertion
below is that criterion in a specific disguise.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from mf_faq.retrieval.resolve import (
    DOMAIN_TERMS,
    ENGLISH_STOPWORDS,
    STOPWORDS,
    Resolution,
    SchemeResolver,
    get_resolver,
    resolve_scheme,
    tokenise,
)
from mf_faq.settings import get_sources

EVAL_DIR = pathlib.Path(__file__).parent.parent / "eval"


@pytest.fixture
def resolver() -> SchemeResolver:
    return SchemeResolver(get_sources())


def _load(name: str) -> list[dict]:
    return yaml.safe_load((EVAL_DIR / name).read_text(encoding="utf-8"))["cases"]


class TestResolvesTheObviousCases:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            (
                "What is the NAV of Motilal Oswal Large and Midcap Fund Direct Growth?",
                "mo_large_midcap",
            ),
            ("expense ratio of Motilal Oswal ELSS Tax Saver Fund", "mo_elss"),
            ("Motilal Oswal Nifty Next 50 Index Fund direct growth NAV", "mo_next50"),
            ("Motilal Oswal BSE Enhanced Value Index Fund", "mo_bse_value"),
            ("Motilal Oswal BSE Financials ex Bank 30 Index Fund", "mo_bse_fin"),
        ],
    )
    def test_full_names_resolve(self, resolver, query, expected):
        match = resolver.resolve(query)
        assert match.resolved
        assert match.scheme_id == expected

    def test_renamed_fund_resolves_under_its_old_name(self, resolver):
        """P0.2: the slug still says 'Most Focused Long Term Fund'."""
        assert resolver.resolve("NAV of Motilal Oswal Most Focused Long Term Fund").scheme_id == (
            "mo_elss"
        )

    def test_misspelling_still_resolves(self, resolver):
        """Q-09 — fuzzy match when only one fund is plausible."""
        match = resolver.resolve("expense ratio of Motilul Oswal larg and midcap fund")
        assert match.resolved
        assert match.scheme_id == "mo_large_midcap"

    def test_short_alias_resolves(self, resolver):
        assert resolver.resolve("What is the lock-in for elss?").scheme_id == "mo_elss"


class TestRefusesToGuess:
    """The whole point. A wrong resolution trips no later guardrail."""

    def test_two_bse_funds_are_ambiguous_not_ranked(self, resolver):
        """Q-08 — the dominant cross-scheme risk (ARCH §6.1)."""
        match = resolver.resolve("What is the expense ratio of the Motilal Oswal BSE index fund?")
        assert match.outcome is Resolution.AMBIGUOUS
        assert match.scheme_id is None
        assert set(match.candidates) == {"mo_bse_value", "mo_bse_fin"}

    def test_generic_index_fund_lists_all_three(self, resolver):
        match = resolver.resolve("Minimum SIP for the Motilal Oswal index fund")
        assert match.outcome is Resolution.AMBIGUOUS
        assert set(match.candidates) == {"mo_bse_value", "mo_next50", "mo_bse_fin"}

    @pytest.mark.parametrize(
        "query",
        [
            "What is the expense ratio?",
            "What's the NAV today?",
            "And its exit load?",
            "",
            "   ",
            "Is this a good fund?",
        ],
    )
    def test_no_scheme_named(self, resolver, query):
        match = resolver.resolve(query)
        assert match.outcome is Resolution.NO_SCHEME
        assert match.scheme_id is None
        assert match.candidates == ()

    def test_out_of_corpus_fund_does_not_resolve(self, resolver):
        """Q-10 — an AMC we do not cover must not land on one we do."""
        for query in [
            "What is the NAV of HDFC Top 100 Fund?",
            "SBI Bluechip Fund expense ratio",
            "Axis Long Term Equity Fund lock-in",
        ]:
            assert not resolver.resolve(query).resolved, query

    def test_a_same_amc_fund_outside_the_corpus_does_not_resolve(self, resolver):
        """⚠ Live bug caught in P4: same AMC, real fund, not one of our 5.

        "Motilal Oswal Midcap Fund" is a plausible real fund name that is NOT
        one of the 5 in this corpus. It matched only the single word "midcap" —
        split out of "Large and Midcap Fund" — and resolved with full confidence
        to mo_large_midcap before MIN_TOKENS_TO_RESOLVE existed. A refusal.yaml
        hard-gate case (R-oob-other-motilal) caught it.
        """
        match = resolver.resolve("What is the NAV of Motilal Oswal Midcap Fund?")
        assert not match.resolved, (
            f"resolved to {match.scheme_id} on {list(match.matched_tokens)} — "
            "a single incidental token must not name a fund"
        )

    def test_two_tokens_still_resolve(self, resolver):
        """The fix must not blind genuine multi-token evidence."""
        match = resolver.resolve("large midcap fund NAV")
        assert match.resolved and match.scheme_id == "mo_large_midcap"

    def test_tokens_from_two_schemes_do_not_resolve_to_either(self, resolver):
        match = resolver.resolve("compare the large midcap fund and the elss fund")
        assert match.outcome is Resolution.AMBIGUOUS
        assert set(match.candidates) == {"mo_large_midcap", "mo_elss"}


class TestTheStopwordTrap:
    """Words that sit in exactly one fund's name but mean nothing as fund names.

    Each case below was a live wrong-scheme resolution before the word lists
    existed. `test_out_of_corpus_fund_does_not_resolve` covers the worst of
    them — an Axis fund answered with Motilal Oswal ELSS data.
    """

    @pytest.mark.parametrize(
        ("query", "would_have_matched"),
        [
            ("What is the expense ratio and the exit load?", "mo_large_midcap"),
            ("What is the net asset value?", "mo_bse_value"),
            ("What are the long term returns?", "mo_elss"),
        ],
    )
    def test_domain_words_do_not_resolve_a_scheme(self, resolver, query, would_have_matched):
        match = resolver.resolve(query)
        assert match.outcome is Resolution.NO_SCHEME, (
            f"resolved to {match.scheme_id} on {list(match.matched_tokens)} — "
            f"the word lists have regressed (this used to hit {would_have_matched})"
        )

    def test_and_is_a_stopword(self):
        assert "and" in STOPWORDS

    def test_the_two_lists_are_separately_named(self):
        assert STOPWORDS == ENGLISH_STOPWORDS | DOMAIN_TERMS
        assert "value" in DOMAIN_TERMS and "value" not in ENGLISH_STOPWORDS

    def test_tax_and_saver_are_deliberately_kept(self, resolver):
        """Removing them would cost the only reading of "Motilal Oswal Tax Saver Fund"."""
        assert not ({"tax", "saver"} & STOPWORDS)
        assert resolver.resolve("Motilal Oswal Tax Saver Fund").scheme_id == "mo_elss"

    def test_every_scheme_still_resolves_from_its_own_names(self, resolver):
        """The guard on over-stripping: a word list may not blind a scheme.

        Each removal from `DOMAIN_TERMS` trades recall for safety, so this
        asserts the trade never went too far — every display name and every
        alias must still resolve to the scheme that declared it.
        """
        for scheme in get_sources().schemes:
            for name in [scheme.display_name, *scheme.aliases]:
                match = resolver.resolve(name)
                assert match.resolved and match.scheme_id == scheme.scheme_id, (
                    f"'{name}' no longer resolves to {scheme.scheme_id} "
                    f"(got {match.outcome.value}/{match.scheme_id or list(match.candidates)}) — "
                    "a stopword addition has blinded a scheme"
                )

    def test_stopwords_are_dropped_by_the_tokeniser(self):
        # "nav" is stripped here as a domain term. "fund" survives the tokeniser
        # and is dropped later by document frequency instead — the two mechanisms
        # are separate, and this pins which one handles which.
        assert tokenise("What is the NAV of the Midcap fund?") == ["midcap", "fund"]

    def test_stopwords_are_absent_from_the_vocabulary(self, resolver):
        assert not (set(resolver.vocabulary) & STOPWORDS)


class TestVocabularyIsDerivedNotHardcoded:
    def test_shared_branding_is_excluded_by_document_frequency(self, resolver):
        for token in ("motilal", "oswal", "fund", "direct", "growth"):
            assert token in resolver.shared_tokens
            assert token not in resolver.vocabulary

    def test_shared_tokens_alone_never_resolve(self, resolver):
        assert resolver.resolve("Motilal Oswal Direct Growth Fund").outcome is Resolution.NO_SCHEME

    def test_distinctive_tokens_are_present(self, resolver):
        for token in ("elss", "midcap", "nifty", "enhanced", "financials"):
            assert token in resolver.vocabulary

    def test_adding_a_scheme_rederives_the_shared_set(self):
        """DF is computed, so a sixth scheme must not need a code change."""
        raw = get_sources().model_dump(mode="json")
        extra = dict(raw["schemes"][0])
        extra.update(
            scheme_id="zz_test",
            display_name="Zenith Quantum Fund Direct Growth",
            aliases=[],
            url="https://groww.in/mutual-funds/zenith-quantum-fund-direct-growth",
        )
        raw["schemes"] = [*raw["schemes"], extra]
        from mf_faq.schemas import SourcesConfig

        resolver = SchemeResolver(SourcesConfig.model_validate(raw))
        # "motilal" no longer appears in every scheme, so it becomes distinctive.
        assert "motilal" in resolver.vocabulary
        assert resolver.resolve("Zenith Quantum Fund").scheme_id == "zz_test"


class TestFuzzBoundaries:
    def test_short_tokens_must_match_exactly(self, resolver):
        """50 vs 30 scores 0.5 — the two BSE funds are told apart by those digits."""
        match = resolver.resolve("Motilal Oswal BSE Financials ex Bank 50 Index Fund")
        assert match.scheme_id != "mo_next50"

    def test_a_two_character_token_alone_is_not_enough_evidence(self, resolver):
        """'ex' is distinctive by DF but cannot name a fund on its own."""
        assert resolver.resolve("ex").outcome is Resolution.NO_SCHEME

    def test_exit_does_not_fuzzy_match_ex(self, resolver):
        assert resolver.resolve("what is the exit load").outcome is Resolution.NO_SCHEME

    def test_fact_words_do_not_match_scheme_words(self, resolver):
        for query in ["expense ratio", "net asset value", "minimum sip", "portfolio holdings"]:
            assert resolver.resolve(query).outcome is Resolution.NO_SCHEME, query


class TestEvidenceIsReported:
    def test_matched_tokens_explain_a_resolution(self, resolver):
        match = resolver.resolve("NAV of the Motilal Oswal ELSS Tax Saver Fund")
        assert "elss" in match.matched_tokens or match.matched_tokens == ()
        assert match.reason

    def test_candidates_are_in_config_order(self, resolver):
        """Stable ordering so a clarification prompt does not reshuffle run to run."""
        match = resolver.resolve("Motilal Oswal index fund")
        assert list(match.candidates) == ["mo_bse_value", "mo_next50", "mo_bse_fin"]


class TestAgainstTheAuthoredEvalSets:
    """P0.7/P0.8 authored these before any of this code existed."""

    @pytest.mark.parametrize("case", _load("ambiguity.yaml"), ids=lambda c: c["id"])
    def test_ambiguity_set(self, case):
        match = resolve_scheme(case["question"])
        expect = case["expect"]
        if expect.get("clarify"):
            assert not match.resolved, f"{case['id']} guessed {match.scheme_id}"
            if expect.get("must_list_candidates"):
                assert set(match.candidates) == set(case["candidates"])
        else:
            assert match.resolved and match.scheme_id == expect["resolves_to"]

    @pytest.mark.parametrize("case", _load("golden.yaml"), ids=lambda c: c["id"])
    def test_golden_set_resolves_to_the_authored_scheme(self, case):
        match = resolve_scheme(case["question"])
        assert match.resolved, f"{case['id']} did not resolve: {match.reason}"
        assert match.scheme_id == case["scheme_id"]

    @pytest.mark.parametrize("case", _load("refusal.yaml"), ids=lambda c: c["id"])
    def test_refusal_set_never_crashes_the_resolver(self, case):
        """Jailbreaks and advisory prompts are refused in P4 — here they must not raise."""
        assert resolve_scheme(case["question"]).outcome in set(Resolution)


class TestResolverCaching:
    def test_get_resolver_is_cached(self):
        assert get_resolver() is get_resolver()
