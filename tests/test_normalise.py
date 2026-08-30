"""Normalisation tests (P2.3).

Runs against the Phase 0 fixtures, never the live site (ARCH §15.7).

The load-bearing test is `test_volatile_html_does_not_change_the_payload_hash`:
it encodes the measured finding that raw HTML differs on every fetch while the
payload does not. If that ever fails, the daily-cadence cost argument is gone
and the P2 hard gate cannot be met.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mf_faq.ingest.normalise import (
    BARRED_FIELDS,
    NormaliseError,
    canonical_json,
    content_hash,
    extract_payload,
    normalise,
    strip_barred,
)

FIXTURES = Path(__file__).parent / "fixtures" / "groww"
SCHEME_IDS = ["mo_large_midcap", "mo_bse_value", "mo_elss", "mo_next50", "mo_bse_fin"]


def payload_for(scheme_id: str) -> dict:
    return json.loads((FIXTURES / f"{scheme_id}.json").read_text(encoding="utf-8"))


def page_html(payload: dict, *, nonce: str = "abc123", cfemail: str = "660b0015") -> str:
    """Rebuild a page around a payload, with the two known volatile elements."""
    blob = json.dumps({"props": {"pageProps": {"mfServerSideData": payload}}})
    return (
        "<html><head><title>Fund</title></head><body>"
        f'<span class="__cf_email__" data-cfemail="{cfemail}">[email&#160;protected]</span>'
        f'<script id="__NEXT_DATA__" type="application/json" nonce="{nonce}" '
        f'crossorigin="anonymous">{blob}</script>'
        "</body></html>"
    )


# -- the finding this module exists for -----------------------------------


def test_volatile_html_does_not_change_the_payload_hash():
    """Measured: two fetches 4s apart differ in HTML but not in payload.

    The entire difference was 52 chars of Cloudflare data-cfemail, which is
    re-obfuscated on every response. Stripping the nonce alone would NOT be
    enough — this test pins both.
    """
    payload = payload_for("mo_elss")
    a = page_html(payload, nonce="fSnmd0lF32iq", cfemail="660b00150e1f0203")
    b = page_html(payload, nonce="d+IjGglGcDvv", cfemail="d5b8b3a6bdacb1b0")

    assert a != b, "fixture setup must actually differ"
    assert content_hash(normalise(a).payload) == content_hash(normalise(b).payload)


def test_raw_html_hashing_would_have_been_wrong():
    """Documents why we do not hash the page — the naive approach fails."""
    payload = payload_for("mo_elss")
    a = page_html(payload, cfemail="aaaa1111")
    b = page_html(payload, cfemail="bbbb2222")
    assert content_hash(a) != content_hash(b)  # the trap
    assert content_hash(normalise(a).payload) == content_hash(normalise(b).payload)  # the fix


# -- extraction ------------------------------------------------------------


@pytest.mark.parametrize("scheme_id", SCHEME_IDS)
def test_extracts_payload_for_every_scheme(scheme_id):
    page = normalise(page_html(payload_for(scheme_id)))
    assert page.payload["scheme_name"]
    assert page.payload["nav_date"]


def test_regex_tolerates_attribute_order():
    """The nonce sits between `id` and `>`; anchoring on id alone is what works."""
    blob = json.dumps({"props": {"pageProps": {"mfServerSideData": {"nav": 1.0}}}})
    variants = [
        f'<script id="__NEXT_DATA__" type="application/json">{blob}</script>',
        f'<script type="application/json" id="__NEXT_DATA__" nonce="x">{blob}</script>',
        f'<script nonce="y" crossorigin="anonymous" id="__NEXT_DATA__">{blob}</script>',
    ]
    for html in variants:
        assert extract_payload(html) == {"nav": 1.0}


def test_missing_script_fails_loudly():
    """A structure change breaks all five schemes at once — never a silent skip."""
    with pytest.raises(NormaliseError, match="page structure has changed"):
        extract_payload("<html><body>no script here</body></html>")


def test_malformed_json_fails_loudly():
    with pytest.raises(NormaliseError, match="not valid JSON"):
        extract_payload('<script id="__NEXT_DATA__">{broken</script>')


def test_missing_payload_path_fails_loudly():
    html = '<script id="__NEXT_DATA__">{"props":{"pageProps":{}}}</script>'
    with pytest.raises(NormaliseError, match="missing at 'mfServerSideData'"):
        extract_payload(html)


def test_empty_payload_fails_loudly():
    html = '<script id="__NEXT_DATA__">{"props":{"pageProps":{"mfServerSideData":{}}}}</script>'
    with pytest.raises(NormaliseError, match="empty or not an object"):
        extract_payload(html)


# -- barred fields (PS §5.3 / P0.4) ---------------------------------------


@pytest.mark.parametrize("scheme_id", SCHEME_IDS)
def test_barred_fields_are_stripped_from_every_scheme(scheme_id):
    """Defence in depth: even whole-payload mishandling cannot leak these."""
    page = normalise(page_html(payload_for(scheme_id)))
    for field in BARRED_FIELDS:
        assert field not in page.payload


def test_actually_removes_the_real_barred_content():
    """These exist in the live payload — the test is meaningless if they don't."""
    raw = payload_for("mo_elss")
    assert raw["groww_rating"] is not None
    assert raw["analysis"], "fixture should carry PROS/CONS text"

    clean, removed = strip_barred(raw)
    assert "groww_rating" in removed
    assert "analysis" in removed
    assert "peerComparison" in removed
    assert "groww_rating" not in clean


def test_in_scope_facts_survive_stripping():
    clean, _ = strip_barred(payload_for("mo_elss"))
    for key in ("nav", "nav_date", "expense_ratio", "exit_load", "benchmark", "holdings"):
        assert key in clean


def test_normalise_reports_what_it_removed():
    page = normalise(page_html(payload_for("mo_elss")))
    assert "groww_rating" in page.barred_removed
    assert page.barred_removed == tuple(sorted(page.barred_removed))


# -- canonicalisation ------------------------------------------------------


def test_key_order_does_not_affect_the_hash():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


def test_list_order_does_not_affect_the_hash():
    """A re-ordered holdings list with identical content is not a change."""
    a = [
        {"company_name": "Zomato", "corpus_per": 5.41},
        {"company_name": "Infosys", "corpus_per": 3.2},
    ]
    assert content_hash(a) == content_hash(list(reversed(a)))


def test_changed_value_does_change_the_hash():
    """The order-insensitivity above must not blind us to real changes."""
    a = [{"company_name": "Zomato", "corpus_per": 5.41}]
    b = [{"company_name": "Zomato", "corpus_per": 5.42}]
    assert content_hash(a) != content_hash(b)


def test_float_representations_normalise():
    """JSON parsing collapses 0.920 and 0.92 to the same float."""
    assert content_hash(json.loads("0.920")) == content_hash(json.loads("0.92"))


def test_canonical_json_is_compact_and_sorted():
    out = canonical_json({"b": 1, "a": {"d": 2, "c": 3}})
    assert out == '{"a":{"c":3,"d":2},"b":1}'


def test_hash_is_stable_across_calls():
    payload = payload_for("mo_next50")
    assert content_hash(payload) == content_hash(payload)


# -- projection (feeds per-fact hashing in 2.7) ---------------------------


def test_project_selects_only_requested_keys():
    page = normalise(page_html(payload_for("mo_elss")))
    projected = page.project(["nav", "nav_date"])
    assert set(projected) == {"nav", "nav_date"}


def test_project_tolerates_absent_keys():
    """lock_in is null for non-ELSS schemes; projection must not KeyError."""
    page = normalise(page_html(payload_for("mo_next50")))
    assert page.project(["nav", "nonexistent_field"]) == {"nav": page.payload["nav"]}


def test_per_fact_hashes_are_independent():
    """Exit criterion: a NAV change must not mark other facts as changed.

    This is why change detection hashes per fact rather than per page — NAV
    moves every business day and would otherwise re-embed the whole corpus.
    """
    page = normalise(page_html(payload_for("mo_elss")))
    before_nav = content_hash(page.project(["nav"]))
    before_bench = content_hash(page.project(["benchmark"]))

    moved = dict(page.payload, nav=99.9999)
    after_nav = content_hash({"nav": moved["nav"]})
    after_bench = content_hash({"benchmark": moved["benchmark"]})

    assert after_nav != before_nav
    assert after_bench == before_bench


def test_page_fingerprint_is_page_wide_not_per_fact():
    """Documents why fingerprint() must not be used for change detection."""
    page = normalise(page_html(payload_for("mo_elss")))
    moved = normalise(page_html(dict(payload_for("mo_elss"), nav=99.9999)))
    assert page.fingerprint() != moved.fingerprint()


def test_records_raw_size():
    page = normalise(page_html(payload_for("mo_elss")))
    assert page.raw_bytes > 1000
