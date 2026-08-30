"""Fact parser tests (P2.4 + P2.5).

Runs against the Phase 0 fixtures — never the live site (ARCH §15.7).
Expected values are cross-checked against docs/phase0-findings.md.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from mf_faq.ingest.parse.groww_scheme_page import (
    EXTRACTORS,
    FactUnavailable,
    SchemeIdentityMismatch,
    extract_holdings,
    extract_lock_in,
    parse_facts,
    parse_iso_date,
    verify_scheme_identity,
)
from mf_faq.settings import get_sources

FIXTURES = Path(__file__).parent / "fixtures" / "groww"
ALL_FACTS = ("nav", "expense_ratio", "exit_load", "holdings", "min_sip", "lock_in", "benchmark")
TODAY = date(2026, 8, 30)  # fixtures were captured on this date


def payload(scheme_id: str) -> dict:
    return json.loads((FIXTURES / f"{scheme_id}.json").read_text(encoding="utf-8"))


def url_for(scheme_id: str) -> str:
    return str(get_sources().scheme(scheme_id).url)


def parse(scheme_id: str, doc_types=ALL_FACTS, **kw):
    return parse_facts(
        payload(scheme_id),
        scheme_id=scheme_id,
        source_url=url_for(scheme_id),
        doc_types=doc_types,
        today=kw.pop("today", TODAY),
        **kw,
    )


def by_type(facts) -> dict:
    return {f.doc_type: f for f in facts}


# -- coverage across all five schemes --------------------------------------


@pytest.mark.parametrize(
    "scheme_id",
    ["mo_large_midcap", "mo_bse_value", "mo_elss", "mo_next50", "mo_bse_fin"],
)
def test_all_seven_facts_extract_for_every_scheme(scheme_id):
    facts, failures = parse(scheme_id)
    assert not failures, [f.reason for f in failures]
    assert set(by_type(facts)) == set(ALL_FACTS)


@pytest.mark.parametrize(
    ("scheme_id", "nav", "ter", "benchmark", "min_sip"),
    [
        ("mo_large_midcap", 41.7091, 0.92, "NIFTY Large Midcap 250 TRI", 500),
        ("mo_bse_value", 27.4719, 0.52, "BSE Enhanced Value TRI", 500),
        ("mo_elss", 67.5754, 0.97, "NIFTY 500 TRI", 500),
        ("mo_next50", 26.6778, 0.41, "NIFTY Next 50 TRI", 500),
        ("mo_bse_fin", 18.3384, 0.52, "BSE Financials ex Bank 30 TRI", 500),
    ],
)
def test_values_match_phase0_findings(scheme_id, nav, ter, benchmark, min_sip):
    facts = by_type(parse(scheme_id)[0])
    assert facts["nav"].value == nav
    assert facts["expense_ratio"].value == ter
    assert facts["benchmark"].value == benchmark
    assert facts["min_sip"].value == min_sip


# -- the timezone trap -----------------------------------------------------


def test_utc_portfolio_date_converts_to_ist_month_end():
    """2026-07-30T18:30:00Z is 00:00 IST on 31 July — the month-end date.

    Reading it naively gives 30 July: off by one, and on the wrong month-end.
    """
    assert parse_iso_date("2026-07-30T18:30:00.000Z") == date(2026, 7, 31)


def test_naive_timestamps_are_not_shifted():
    """`2026-08-28T00:00:00` has no timezone — already IST-local."""
    assert parse_iso_date("2026-08-28T00:00:00") == date(2026, 8, 28)
    assert parse_iso_date("2022-07-29T05:30:00") == date(2022, 7, 29)


def test_holdings_date_uses_ist_conversion():
    fact = by_type(parse("mo_elss")[0])["holdings"]
    assert fact.source_as_of == date(2026, 7, 31)


def test_unparseable_iso_date_raises():
    with pytest.raises(ValueError, match="unparseable ISO date"):
        parse_iso_date("not-a-date")


# -- source_as_of per fact (P2.5) -----------------------------------------


def test_each_fact_carries_its_own_date():
    """Facts on one page do not share a date — they update independently."""
    facts = by_type(parse("mo_elss")[0])
    assert facts["nav"].source_as_of == date(2026, 8, 28)
    assert facts["expense_ratio"].source_as_of == date(2026, 8, 28)
    assert facts["holdings"].source_as_of == date(2026, 7, 31)
    assert facts["exit_load"].source_as_of == date(2014, 12, 26)


def test_page_level_dated_facts_are_flagged():
    """PS §9 — these take nav_date and are gate-exempt; the flag records that."""
    facts = by_type(parse("mo_elss")[0])
    for name in ("min_sip", "lock_in", "benchmark"):
        assert facts[name].date_is_page_level is True
        assert facts[name].source_as_of == date(2026, 8, 28)
    for name in ("nav", "expense_ratio", "exit_load", "holdings"):
        assert facts[name].date_is_page_level is False


def test_fact_with_no_date_is_rejected():
    """Exit criterion: an undated fact is uncitable, so it is dropped (ARCH §11)."""
    p = copy.deepcopy(payload("mo_elss"))
    p["historic_fund_expense"] = []
    facts, failures = parse_facts(
        p,
        scheme_id="mo_elss",
        source_url=url_for("mo_elss"),
        doc_types=["expense_ratio"],
        today=TODAY,
    )
    assert facts == []
    assert "no historic_fund_expense" in failures[0].reason


def test_future_date_is_rejected():
    """A source_as_of ahead of today is a parser bug, never real data (F-04)."""
    facts, failures = parse("mo_elss", ["nav"], today=date(2026, 8, 1))
    assert facts == []
    assert "in the future" in failures[0].reason


def test_one_bad_fact_does_not_sink_the_others():
    """A coverage gap is not a run failure — other facts still land."""
    p = copy.deepcopy(payload("mo_elss"))
    p["nav"] = None
    facts, failures = parse_facts(
        p,
        scheme_id="mo_elss",
        source_url=url_for("mo_elss"),
        doc_types=ALL_FACTS,
        today=TODAY,
    )
    assert len(facts) == len(ALL_FACTS) - 1
    assert [f.doc_type for f in failures] == ["nav"]


# -- scheme identity (I-06) ------------------------------------------------


def test_identity_passes_for_every_scheme():
    for scheme_id in ["mo_large_midcap", "mo_bse_value", "mo_elss", "mo_next50", "mo_bse_fin"]:
        verify_scheme_identity(payload(scheme_id), url_for(scheme_id))


def test_renamed_elss_fund_still_verifies():
    """P0.2 — the fund was renamed but search_id still tracks the URL slug."""
    p = payload("mo_elss")
    assert p["scheme_name"] == "Motilal Oswal ELSS Tax Saver Fund Direct Growth"
    assert "most-focused-long-term-fund" in p["search_id"]
    verify_scheme_identity(p, url_for("mo_elss"))


def test_wrong_scheme_payload_is_rejected():
    """A redirect would file one fund's data under another's scheme_id."""
    with pytest.raises(SchemeIdentityMismatch, match="wrong scheme"):
        verify_scheme_identity(payload("mo_next50"), url_for("mo_elss"))


def test_missing_search_id_is_rejected():
    p = copy.deepcopy(payload("mo_elss"))
    p["search_id"] = None
    with pytest.raises(SchemeIdentityMismatch, match="cannot verify identity"):
        verify_scheme_identity(p, url_for("mo_elss"))


def test_parse_facts_checks_identity_before_extracting():
    with pytest.raises(SchemeIdentityMismatch):
        parse_facts(
            payload("mo_next50"),
            scheme_id="mo_elss",
            source_url=url_for("mo_elss"),
            doc_types=["nav"],
            today=TODAY,
        )


# -- lock_in: "none" is an answer, not a gap ------------------------------


def test_elss_lock_in_is_three_years():
    value = by_type(parse("mo_elss")[0])["lock_in"].value
    assert value == {"has_lock_in": True, "years": 3, "months": 0, "days": 0}


@pytest.mark.parametrize("scheme_id", ["mo_large_midcap", "mo_next50", "mo_bse_fin"])
def test_absent_lock_in_means_none_not_missing(scheme_id):
    """All-null lock_in is a real answer: "no lock-in".

    Treating it as absent would refuse "is there a lock-in on the Next 50
    fund?", where the correct answer is "no" (golden case G-lockin-next50-none).
    """
    fact = by_type(parse(scheme_id)[0])["lock_in"]
    assert fact.value == {"has_lock_in": False}


def test_lock_in_wrong_shape_is_rejected():
    with pytest.raises(FactUnavailable, match="unexpected lock_in shape"):
        extract_lock_in({"lock_in": "3 years", "nav_date": "28-Aug-2026"})


# -- holdings: verbatim, one value (PS §8.2, R-08) ------------------------


def test_holdings_returned_as_one_value_not_per_holding():
    """Per-holding chunks would truncate a top-holdings answer at top_k=4."""
    fact = by_type(parse("mo_elss")[0])["holdings"]
    assert fact.value["count"] == 32
    assert len(fact.value["holdings"]) == 32


def test_holdings_sorted_by_weight_descending():
    entries = by_type(parse("mo_large_midcap")[0])["holdings"].value["holdings"]
    weights = [e["corpus_per"] for e in entries]
    assert weights == sorted(weights, reverse=True)


def test_holdings_carry_only_name_and_weight():
    """Verbatim disclosure only — no sector, no derived commentary (PS §8.2)."""
    entries = by_type(parse("mo_elss")[0])["holdings"].value["holdings"]
    assert set(entries[0]) == {"company_name", "corpus_per"}


def test_holdings_skips_entries_missing_name_or_weight():
    p = copy.deepcopy(payload("mo_elss"))
    p["holdings"][0]["corpus_per"] = None
    value, _, _ = extract_holdings(p)
    assert value["count"] == 31


def test_holdings_absent_is_rejected():
    with pytest.raises(FactUnavailable, match="absent or empty"):
        extract_holdings({"holdings": []})


# -- null markers (I-03) ---------------------------------------------------


@pytest.mark.parametrize("marker", [None, "", "  ", "N/A", "n/a", "-", "--", "None"])
def test_null_markers_reject_the_fact(marker):
    """Storing a placeholder would make the assistant state "N/A" as a fact."""
    p = copy.deepcopy(payload("mo_elss"))
    p["benchmark"] = marker
    facts, failures = parse_facts(
        p,
        scheme_id="mo_elss",
        source_url=url_for("mo_elss"),
        doc_types=["benchmark"],
        today=TODAY,
    )
    assert facts == []
    assert "absent or empty" in failures[0].reason


def test_nil_exit_load_is_a_real_value_not_a_null():
    """The ELSS fund's exit load IS "Nil" — a meaningful answer, not a gap."""
    fact = by_type(parse("mo_elss")[0])["exit_load"]
    assert fact.value == "Nil"


# -- out of corpus (P0.4) --------------------------------------------------


def test_no_riskometer_extractor_exists():
    """P0.4: nfo_risk and return_stats[].risk disagree and neither is dated."""
    assert "riskometer" not in EXTRACTORS


def test_requesting_an_out_of_corpus_fact_fails_gracefully():
    facts, failures = parse("mo_elss", ["riskometer"])
    assert facts == []
    assert "no extractor" in failures[0].reason


def test_extractors_match_the_configured_allowlist():
    """Every fact sources.yaml asks for must have an extractor, and vice versa."""
    configured = set(get_sources().scheme("mo_elss").extract)
    assert configured == set(EXTRACTORS)


# -- identity of the fact record ------------------------------------------


def test_doc_id_is_scheme_and_type():
    fact = by_type(parse("mo_elss")[0])["nav"]
    assert fact.doc_id == "mo_elss:nav"
    assert fact.source_url == url_for("mo_elss")
