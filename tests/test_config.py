"""Config validation tests (P1 exit criterion).

The positive tests confirm the real configs load. The negative tests matter
more: each proves a specific misconfiguration fails LOUDLY AT BOOT rather than
silently at 23:30 or, worse, on a user's question.
"""

from __future__ import annotations

import copy

import pytest
import yaml
from pydantic import ValidationError

from mf_faq.schemas import RefusalLinksConfig, SourcesConfig
from mf_faq.settings import CONFIG_DIR, get_refusal_links, get_sources


@pytest.fixture
def raw_sources() -> dict:
    return yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def raw_links() -> dict:
    return yaml.safe_load((CONFIG_DIR / "refusal_links.yaml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Positive: the shipped configs are valid and match Phase 0 findings
# --------------------------------------------------------------------------


def test_sources_config_loads():
    cfg = get_sources()
    assert len(cfg.schemes) == 5
    assert cfg.fact_source.domain == "groww.in"


def test_all_five_schemes_present():
    ids = {s.scheme_id for s in get_sources().schemes}
    assert ids == {"mo_large_midcap", "mo_bse_value", "mo_elss", "mo_next50", "mo_bse_fin"}


def test_every_scheme_url_is_on_the_fact_domain():
    """PS §5.1 — Groww is the sole fact source; no fallback may creep back in."""
    for scheme in get_sources().schemes:
        assert (scheme.url.host or "").removeprefix("www.") == "groww.in"


def test_riskometer_and_statement_are_out_of_corpus():
    """P0.4 exclusions must be declared, not silently missing."""
    excluded = {f.fact for f in get_sources().out_of_corpus}
    assert {"riskometer", "statement_process"} <= excluded


def test_nav_refuses_past_max_age_but_others_flag():
    """PS §8.6 as decided in P0.9."""
    dt = get_sources().doc_types
    assert dt["nav"].past_max_age == "refuse"
    assert dt["nav"].max_age_days == 1
    assert dt["expense_ratio"].past_max_age == "flag"


def test_page_level_date_facts_are_gate_exempt():
    """PS §9 — nav_date advances daily, so max_age can never fire for these."""
    dt = get_sources().doc_types
    for name in ("min_sip", "lock_in", "benchmark"):
        assert dt[name].date_is_page_level is True
        assert dt[name].past_max_age == "exempt"
        assert dt[name].max_age_days is None


def test_renamed_elss_fund_resolves_under_both_names():
    """P0.2 — the fund was renamed; the old slug name must still resolve."""
    aliases = {a.casefold() for a in get_sources().scheme("mo_elss").aliases}
    assert "motilal oswal elss tax saver fund" in aliases
    assert "motilal oswal most focused long term fund" in aliases


def test_barred_fields_are_not_extracted():
    """P0.4 — the payload carries ratings/returns/PROS-CONS; none may be ingested."""
    barred = {"groww_rating", "analysis", "peer_comparison", "returns", "fund_manager"}
    for scheme in get_sources().schemes:
        assert not (set(scheme.extract) & barred)


# --------------------------------------------------------------------------
# Negative: each proves a real misconfiguration is caught at boot
# --------------------------------------------------------------------------


def test_rejects_non_groww_fact_url(raw_sources):
    """A stray AMC/AMFI URL would silently reintroduce the fallback corpus."""
    bad = copy.deepcopy(raw_sources)
    bad["schemes"][0]["url"] = "https://www.amfiindia.com/spages/NAVAll.txt"
    with pytest.raises(ValidationError, match="not the permitted fact domain"):
        SourcesConfig.model_validate(bad)


def test_rejects_extract_without_doc_type_policy(raw_sources):
    """An extracted fact with no freshness policy would KeyError at query time."""
    bad = copy.deepcopy(raw_sources)
    bad["schemes"][0]["extract"].append("riskometer_score")
    with pytest.raises(ValidationError, match="no doc_types entry"):
        SourcesConfig.model_validate(bad)


def test_rejects_extracting_an_out_of_corpus_fact(raw_sources):
    """Contradiction: a fact declared refused must not reach the index."""
    bad = copy.deepcopy(raw_sources)
    bad["doc_types"]["riskometer"] = {
        "max_age_days": 45,
        "past_max_age": "flag",
        "date_field": "nfo_risk",
    }
    bad["schemes"][0]["extract"].append("riskometer")
    with pytest.raises(ValidationError, match="declared out_of_corpus"):
        SourcesConfig.model_validate(bad)


def test_rejects_duplicate_scheme_id(raw_sources):
    bad = copy.deepcopy(raw_sources)
    bad["schemes"][1]["scheme_id"] = bad["schemes"][0]["scheme_id"]
    with pytest.raises(ValidationError, match="duplicate scheme_id"):
        SourcesConfig.model_validate(bad)


def test_rejects_alias_collision_across_schemes(raw_sources):
    """Two schemes on one alias is the cross-scheme misattribution risk (ARCH §6.1)."""
    bad = copy.deepcopy(raw_sources)
    bad["schemes"][1]["aliases"].append(bad["schemes"][0]["aliases"][0])
    with pytest.raises(ValidationError, match="would cause wrong-scheme answers"):
        SourcesConfig.model_validate(bad)


def test_rejects_incoherent_freshness_policy(raw_sources):
    """A non-exempt doc_type without max_age_days leaves the gate unable to run."""
    bad = copy.deepcopy(raw_sources)
    bad["doc_types"]["nav"]["max_age_days"] = None
    with pytest.raises(ValidationError, match="requires max_age_days"):
        SourcesConfig.model_validate(bad)


def test_rejects_exempt_policy_with_max_age(raw_sources):
    """A gate that can never fire is a config mistake, not a valid state."""
    bad = copy.deepcopy(raw_sources)
    bad["doc_types"]["min_sip"]["max_age_days"] = 45
    with pytest.raises(ValidationError, match="contradictory"):
        SourcesConfig.model_validate(bad)


def test_rejects_anonymous_user_agent(raw_sources):
    """PS §4.5 behaviour 8 — daily crawling requires an identifying agent."""
    bad = copy.deepcopy(raw_sources)
    bad["fact_source"]["user_agent"] = "bot"
    with pytest.raises(ValidationError, match="identifying string"):
        SourcesConfig.model_validate(bad)


def test_rejects_unknown_top_level_key(raw_sources):
    """extra='forbid' catches typos that would otherwise be silently ignored."""
    bad = copy.deepcopy(raw_sources)
    bad["scheems"] = []
    with pytest.raises(ValidationError):
        SourcesConfig.model_validate(bad)


# --------------------------------------------------------------------------
# Refusal link registry (P0.6)
# --------------------------------------------------------------------------


def test_refusal_links_load():
    cfg = get_refusal_links()
    assert len(cfg.factsheet_links) == 5
    assert "default" in cfg.educational_links


def test_every_scheme_has_a_factsheet_link():
    links, schemes = get_refusal_links(), get_sources()
    for scheme in schemes.schemes:
        assert scheme.scheme_id in links.factsheet_links


def test_performance_refusal_routes_to_factsheet():
    """PS §5.3 — performance queries get the official factsheet."""
    link = get_refusal_links().link_for("performance_barred", "mo_elss")
    assert link is not None
    assert "elss-tax-saver-fund" in str(link.url)


def test_advisory_refusal_never_routes_to_a_product_page():
    """The routing guard: 'should I invest?' must not land on an 'Invest In...' page."""
    links = get_refusal_links()
    product_urls = {str(t.url) for t in links.factsheet_links.values()}
    for reason in ("advisory_direct", "advisory_disguised", "mixed_factual_advisory"):
        link = links.link_for(reason, "mo_elss")
        assert link is not None
        assert str(link.url) not in product_urls
        assert "investor-education" in str(link.url)


def test_pii_and_ambiguity_carry_no_outbound_link():
    links = get_refusal_links()
    assert links.link_for("pii") is None
    assert links.link_for("ambiguous_scheme") is None


def test_unknown_reason_fails_closed_to_educational():
    """A refusal must always render, even for an unmapped reason."""
    link = get_refusal_links().link_for("some_future_reason")
    assert link is not None and "investor-education" in str(link.url)


def test_rejects_link_outside_health_check_domains(raw_links):
    """A link the liveness checker cannot reach would report as dead (I-08)."""
    bad = copy.deepcopy(raw_links)
    bad["educational_links"]["default"]["url"] = "https://example.com/education"
    with pytest.raises(ValidationError, match="not in link_health_domains"):
        RefusalLinksConfig.model_validate(bad)
