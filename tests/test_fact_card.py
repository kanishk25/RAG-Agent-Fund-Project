"""Fact card tests (P2.6).

The load-bearing group is "date never in card text". Two independent facts make
it necessary — PS §9's page-level dates, and `historic_fund_expense`'s Daily
frequency — and violating it would silently re-embed unchanged facts every day.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from mf_faq.ingest.fact_card import render_card, render_cards, render_context
from mf_faq.ingest.parse import parse_facts
from mf_faq.settings import get_sources

FIXTURES = Path(__file__).parent / "fixtures" / "groww"
TODAY = date(2026, 8, 30)
SCHEME_IDS = ["mo_large_midcap", "mo_bse_value", "mo_elss", "mo_next50", "mo_bse_fin"]


def payload(scheme_id: str) -> dict:
    return json.loads((FIXTURES / f"{scheme_id}.json").read_text(encoding="utf-8"))


def cards_for(scheme_id: str, doc_types=None) -> dict:
    scheme = get_sources().scheme(scheme_id)
    facts, _ = parse_facts(
        payload(scheme_id),
        scheme_id=scheme_id,
        source_url=str(scheme.url),
        doc_types=doc_types or scheme.extract,
        today=TODAY,
    )
    return {c.doc_type: c for c in render_cards(facts, scheme)}


# -- the constraint this module exists for --------------------------------


@pytest.mark.parametrize("scheme_id", SCHEME_IDS)
def test_no_card_text_contains_its_date(scheme_id):
    """PS §9 + the Daily-frequency finding: a date in text means a daily re-embed."""
    for card in cards_for(scheme_id).values():
        assert card.source_as_of not in card.text
        # Also reject the display form Groww uses, e.g. "28-Aug-2026".
        d = date.fromisoformat(card.source_as_of)
        assert d.strftime("%d-%b-%Y") not in card.text
        assert str(d.year) not in card.text or card.doc_type == "holdings"


def test_expense_ratio_card_is_stable_while_only_its_date_moves():
    """`historic_fund_expense` is Daily: the ELSS ratio held 0.97 for 48 days
    while `as_on_date` advanced each one. The card must not churn with it."""
    base = payload("mo_elss")
    moved = copy.deepcopy(base)
    for row in moved["historic_fund_expense"]:
        if row["as_on_date"].startswith("2026-08-28"):
            row["as_on_date"] = "2026-08-29T00:00:00"

    a = cards_for("mo_elss", ["expense_ratio"])["expense_ratio"]
    scheme = get_sources().scheme("mo_elss")
    facts, _ = parse_facts(
        moved,
        scheme_id="mo_elss",
        source_url=str(scheme.url),
        doc_types=["expense_ratio"],
        today=date(2026, 8, 31),
    )
    b = render_card(facts[0], scheme)

    assert b.source_as_of != a.source_as_of  # the date really did move
    assert b.text_hash == a.text_hash  # but the embedded text did not


def test_page_level_facts_are_stable_across_nav_date_advance():
    """min_sip / lock_in / benchmark take nav_date, which moves every day."""
    base = payload("mo_elss")
    tomorrow = copy.deepcopy(base)
    tomorrow["nav_date"] = "29-Aug-2026"

    scheme = get_sources().scheme("mo_elss")
    types = ["min_sip", "lock_in", "benchmark"]
    before = cards_for("mo_elss", types)
    facts, _ = parse_facts(
        tomorrow,
        scheme_id="mo_elss",
        source_url=str(scheme.url),
        doc_types=types,
        today=date(2026, 8, 31),
    )
    after = {c.doc_type: c for c in render_cards(facts, scheme)}

    for doc_type in types:
        assert after[doc_type].source_as_of != before[doc_type].source_as_of
        assert after[doc_type].text_hash == before[doc_type].text_hash


def test_a_changed_value_does_change_the_hash():
    """Stability must not become blindness to real change."""
    base = cards_for("mo_elss", ["nav"])["nav"]
    moved = copy.deepcopy(payload("mo_elss"))
    moved["nav"] = 99.1234

    scheme = get_sources().scheme("mo_elss")
    facts, _ = parse_facts(
        moved,
        scheme_id="mo_elss",
        source_url=str(scheme.url),
        doc_types=["nav"],
        today=TODAY,
    )
    assert render_card(facts[0], scheme).text_hash != base.text_hash


# -- card content ----------------------------------------------------------


@pytest.mark.parametrize("scheme_id", SCHEME_IDS)
def test_every_fact_renders_a_card(scheme_id):
    cards = cards_for(scheme_id)
    assert len(cards) == 7


@pytest.mark.parametrize("scheme_id", SCHEME_IDS)
def test_every_card_names_its_scheme(scheme_id):
    """The scheme name is what an embedding matches on; a bare number does not."""
    display = get_sources().scheme(scheme_id).display_name
    for card in cards_for(scheme_id).values():
        assert display in card.text


def test_nav_card_reads_naturally():
    card = cards_for("mo_elss", ["nav"])["nav"]
    assert "₹67.5754" in card.text
    assert "net asset value" in card.text


def test_expense_ratio_card_carries_the_percent():
    assert "0.97%" in cards_for("mo_elss", ["expense_ratio"])["expense_ratio"].text


def test_min_sip_card_formats_rupees():
    assert "₹500" in cards_for("mo_elss", ["min_sip"])["min_sip"].text


def test_lock_in_card_states_both_years_and_months():
    """Grounding (G-08): a correct answer may say "3 years" or "36 months",
    and the validator requires the number to appear verbatim in chunk text."""
    text = cards_for("mo_elss", ["lock_in"])["lock_in"].text
    assert "3 years" in text
    assert "36 months" in text


@pytest.mark.parametrize("scheme_id", ["mo_large_midcap", "mo_next50", "mo_bse_fin"])
def test_absent_lock_in_states_none_explicitly(scheme_id):
    """G-lockin-next50-none expects "no lock-in", not a refusal."""
    assert "no lock-in period" in cards_for(scheme_id, ["lock_in"])["lock_in"].text


def test_exit_load_nil_renders_as_a_statement():
    assert "exit load" in cards_for("mo_elss", ["exit_load"])["exit_load"].text.lower()
    assert "Nil" in cards_for("mo_elss", ["exit_load"])["exit_load"].text


def test_benchmark_card_carries_the_index_name():
    assert "NIFTY 500 TRI" in cards_for("mo_elss", ["benchmark"])["benchmark"].text


# -- holdings: one card, verbatim (PS §8.2, R-08) -------------------------


def test_holdings_render_as_a_single_card():
    """Per-holding cards would truncate a top-holdings answer at top_k=4."""
    card = cards_for("mo_next50", ["holdings"])["holdings"]
    assert card.doc_type == "holdings"
    assert "52 portfolio holdings" in card.text


def test_holdings_card_lists_names_and_weights_verbatim():
    text = cards_for("mo_large_midcap", ["holdings"])["holdings"].text
    assert "Zomato Ltd 5.41%" in text


def test_holdings_card_carries_no_commentary():
    """PS §8.2 — verbatim disclosure only, no derived judgement."""
    text = cards_for("mo_elss", ["holdings"])["holdings"].text.lower()
    for word in ("attractive", "strong", "recommend", "outperform", "well positioned", "quality"):
        assert word not in text


def test_holdings_card_stays_a_reasonable_chunk_size():
    """ARCH §6.3 targets 300-500 token chunks; ~4 chars/token."""
    text = cards_for("mo_next50", ["holdings"])["holdings"].text
    assert len(text) < 2600, f"holdings card is {len(text)} chars — may exceed chunk budget"


# -- metadata --------------------------------------------------------------


def test_metadata_carries_every_query_time_field():
    meta = cards_for("mo_elss", ["nav"])["nav"].metadata()
    assert set(meta) == {
        "doc_id",
        "scheme_id",
        "doc_type",
        "source_url",
        "source_as_of",
        "date_is_page_level",
    }
    assert meta["doc_id"] == "mo_elss:nav"
    assert meta["source_as_of"] == "2026-08-28"


def test_page_level_flag_survives_into_metadata():
    """The freshness gate needs it to know these facts are exempt (PS §9)."""
    cards = cards_for("mo_elss")
    assert cards["benchmark"].metadata()["date_is_page_level"] is True
    assert cards["nav"].metadata()["date_is_page_level"] is False


def test_source_url_is_the_citation_shown_to_users():
    card = cards_for("mo_elss", ["nav"])["nav"]
    assert card.source_url == str(get_sources().scheme("mo_elss").url)
    assert "groww.in" in card.source_url


# -- LLM context (feeds P4) -----------------------------------------------


def test_context_reintroduces_the_date_outside_the_embedded_text():
    """The split that makes both requirements satisfiable: the model sees the
    date (PS §8.7) while the embedding never does."""
    cards = list(cards_for("mo_elss", ["nav", "benchmark"]).values())
    context = render_context(cards)
    assert "source_as_of: 2026-08-28" in context
    for card in cards:
        assert card.source_as_of not in card.text


def test_context_numbers_each_card_and_carries_its_url():
    context = render_context(list(cards_for("mo_elss", ["nav", "benchmark"]).values()))
    assert "[1]" in context and "[2]" in context
    assert context.count("source_url:") == 2


def test_context_of_nothing_is_empty():
    assert render_context([]) == ""


@pytest.mark.parametrize("scheme_id", SCHEME_IDS)
def test_scheme_name_appears_exactly_once_per_card(scheme_id):
    """The name must appear (queries match on it) but not twice.

    Retrieved chunks are re-sent on every request against the 8K TPM ceiling
    (ARCH §15.4), so a duplicated ~46-char name is paid for on each retrieval.
    """
    display = get_sources().scheme(scheme_id).display_name
    for card in cards_for(scheme_id).values():
        assert card.text.count(display) == 1, f"{card.doc_type}: {card.text[:90]}"
