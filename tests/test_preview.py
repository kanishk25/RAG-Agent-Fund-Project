"""Fact-preview tests (P2.10).

`preview` is the one view that runs entirely from saved fixtures, so its whole
value is being free of network and free of side effects. Both properties are
asserted here rather than assumed: a preview that quietly fetched, or quietly
wrote, would be indistinguishable from the real pipeline at the point where
someone reached for it precisely because it is neither.

The `--live` path is covered too, through the offline `groww_fetcher` rig from
conftest. It is the only code in the module that can touch the network, so
leaving it untested would leave the risky half uncovered.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from mf_faq.ingest import preview
from mf_faq.ingest.parse import ExtractedFact
from mf_faq.settings import get_sources

ALL_SCHEMES = [s.scheme_id for s in get_sources().schemes]
DOC_TYPES = ("nav", "expense_ratio", "exit_load", "holdings", "min_sip", "lock_in", "benchmark")
FACTS_PER_SCHEME = len(DOC_TYPES)


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if anything opens a socket during a fixture-mode preview."""
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("preview opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


# -- fixture loading -------------------------------------------------------


@pytest.mark.parametrize("scheme_id", ALL_SCHEMES)
def test_every_configured_scheme_has_a_fixture(scheme_id):
    """Default preview covers the whole corpus, so every scheme needs a fixture."""
    payload = preview.load_fixture(scheme_id)
    assert isinstance(payload, dict)
    assert payload.get("search_id")


def test_missing_fixture_names_the_path_it_looked_for():
    with pytest.raises(FileNotFoundError) as exc:
        preview.load_fixture("no_such_scheme")
    assert "no_such_scheme" in str(exc.value)


# -- render ----------------------------------------------------------------


def _fact(doc_type, value):
    from datetime import date

    return ExtractedFact(
        scheme_id="mo_elss",
        doc_type=doc_type,
        value=value,
        source_as_of=date(2026, 8, 28),
        source_url="https://groww.in/mutual-funds/x",
    )


def test_render_holdings_summarises_count_and_largest_position():
    value = {
        "holdings": [
            {"company_name": "Multi Commodity Exchange", "corpus_per": 7.5},
            {"company_name": "Zomato Ltd", "corpus_per": 5.4},
        ],
        "count": 2,
    }
    assert preview.render(_fact("holdings", value)) == (
        "2 holdings, largest Multi Commodity Exchange @ 7.5%"
    )


def test_render_lock_in_distinguishes_absent_from_zero():
    """`has_lock_in: false` must not render as "0y 0m 0d" — that reads as a real
    lock-in of zero length rather than a fund that has none."""
    assert preview.render(_fact("lock_in", {"has_lock_in": False})) == "no lock-in"
    assert (
        preview.render(_fact("lock_in", {"has_lock_in": True, "years": 3, "months": 0, "days": 0}))
        == "3y 0m 0d"
    )


@pytest.mark.parametrize(
    ("doc_type", "value", "expected"),
    [
        ("expense_ratio", 0.97, "0.97%"),
        ("min_sip", 500, "Rs 500"),
        ("nav", 67.5754, "67.5754"),
        ("benchmark", "NIFTY 500 TRI", "NIFTY 500 TRI"),
        ("exit_load", "Nil", "Nil"),
    ],
)
def test_render_units_and_fallthrough(doc_type, value, expected):
    assert preview.render(_fact(doc_type, value)) == expected


# -- default (fixture) run -------------------------------------------------


def test_default_run_previews_the_whole_corpus_offline(capsys, no_network):
    assert preview.main([]) == 0
    out = capsys.readouterr().out
    assert f"{len(ALL_SCHEMES) * FACTS_PER_SCHEME} facts, 0 unavailable" in out
    assert f"{len(ALL_SCHEMES)} scheme(s)" in out
    for scheme in get_sources().schemes:
        assert scheme.display_name in out


def test_output_says_plainly_that_nothing_was_persisted(capsys, no_network):
    """The whole point of the tool is that it is read-only; the user is told so."""
    preview.main([])
    out = capsys.readouterr().out
    assert "NOT persisted" in out
    assert "fixtures" in out


def test_single_scheme_previews_only_that_scheme(capsys, no_network):
    assert preview.main(["--scheme", "mo_elss"]) == 0
    out = capsys.readouterr().out
    assert f"{FACTS_PER_SCHEME} facts, 0 unavailable, 1 scheme(s)" in out
    assert "ELSS Tax Saver" in out
    assert "Nifty Next 50" not in out


def test_page_level_dates_are_flagged_in_the_human_view(capsys, no_network):
    """min_sip / lock_in / benchmark borrow the page's nav_date (PS §9). A reader
    comparing dates has to be able to see which ones are not publisher dates."""
    preview.main(["--scheme", "mo_elss"])
    lines = {
        line.split()[0]: line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("    ") and line.strip()
    }
    for doc_type in ("min_sip", "lock_in", "benchmark"):
        assert "(page-level date)" in lines[doc_type]
    for doc_type in ("nav", "expense_ratio", "holdings", "exit_load"):
        assert "(page-level date)" not in lines[doc_type]


def test_unknown_scheme_exits_one_with_the_error_on_stderr(capsys, no_network):
    assert preview.main(["--scheme", "bogus"]) == 1
    captured = capsys.readouterr()
    assert "bogus" in captured.err
    assert captured.out == ""


# -- JSON mode -------------------------------------------------------------


def test_json_mode_emits_one_record_per_fact(capsys, no_network):
    assert preview.main(["--json"]) == 0
    records = json.loads(capsys.readouterr().out)
    assert len(records) == len(ALL_SCHEMES) * FACTS_PER_SCHEME
    assert {r["doc_id"] for r in records} == {
        f"{scheme_id}:{doc_type}" for scheme_id in ALL_SCHEMES for doc_type in DOC_TYPES
    }
    for record in records:
        assert record["source_as_of"]
        assert record["source_url"].startswith("https://groww.in/")
        assert "unavailable" not in record


def test_json_output_is_not_polluted_by_log_lines(capsys, no_network, monkeypatch):
    """The P2.9 defect, guarded here too. Dropping a fact makes the parser log a
    WARNING, which is exactly what broke `--json` on the CLI: the log line landed
    on stdout and the stream stopped being JSON. It must go to stderr instead."""
    payload = preview.load_fixture("mo_elss")
    payload.pop("min_sip_investment", None)
    monkeypatch.setattr(preview, "load_fixture", lambda _sid: payload)

    preview.main(["--scheme", "mo_elss", "--json"])
    captured = capsys.readouterr()

    json.loads(captured.out)  # raises if a log line landed on stdout
    assert "fact rejected" in captured.err
    assert "fact rejected" not in captured.out


def test_json_reports_unavailable_facts_with_a_reason(capsys, no_network, monkeypatch):
    """A fact the page does not yield is a coverage gap, not a crash — it has to
    appear in the preview, or the tool would silently under-report the corpus."""
    payload = preview.load_fixture("mo_elss")
    payload.pop("min_sip_investment", None)
    monkeypatch.setattr(preview, "load_fixture", lambda _sid: payload)

    assert preview.main(["--scheme", "mo_elss", "--json"]) == 0
    records = json.loads(capsys.readouterr().out)

    assert len(records) == FACTS_PER_SCHEME
    gaps = [r for r in records if "unavailable" in r]
    assert [r["doc_type"] for r in gaps] == ["min_sip"]
    assert "min_sip_investment" in gaps[0]["unavailable"]
    assert gaps[0]["doc_id"] == "mo_elss:min_sip"
    assert "value" not in gaps[0] and "source_as_of" not in gaps[0]


def test_human_view_marks_unavailable_facts(capsys, no_network, monkeypatch):
    payload = preview.load_fixture("mo_elss")
    payload.pop("min_sip_investment", None)
    monkeypatch.setattr(preview, "load_fixture", lambda _sid: payload)

    preview.main(["--scheme", "mo_elss"])
    out = capsys.readouterr().out
    assert "min_sip" in out and "UNAVAILABLE" in out
    assert f"{FACTS_PER_SCHEME - 1} facts, 1 unavailable" in out


# -- --live ----------------------------------------------------------------


def test_live_mode_goes_through_fetch_and_normalise(capsys, monkeypatch, groww_fetcher):
    """Exercised offline through MockTransport. `--live` is the only path that
    can reach groww.in, so it is the half most worth covering."""
    import mf_faq.ingest.fetch as fetch_module

    monkeypatch.setattr(fetch_module, "build_fetcher", lambda *a, **kw: groww_fetcher())

    assert preview.main(["--live", "--scheme", "mo_elss"]) == 0
    out = capsys.readouterr().out
    assert "LIVE groww.in" in out
    assert f"{FACTS_PER_SCHEME} facts, 0 unavailable" in out


def test_live_and_fixture_modes_agree_on_the_facts(capsys, monkeypatch, groww_fetcher):
    """Normalisation strips barred fields (P2.3) before parsing on the live path.
    If that ever removed a field an extractor needs, the two modes would diverge
    and this is where it shows."""
    import mf_faq.ingest.fetch as fetch_module

    preview.main(["--json"])
    from_fixtures = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(fetch_module, "build_fetcher", lambda *a, **kw: groww_fetcher())
    preview.main(["--live", "--json"])
    from_live = json.loads(capsys.readouterr().out)

    assert from_live == from_fixtures


def test_live_mode_fetches_each_page_once(monkeypatch, groww_fetcher, capsys):
    fetcher = groww_fetcher()
    calls: list[str] = []
    original = fetcher.fetch
    monkeypatch.setattr(
        fetcher, "fetch", lambda url: (calls.append(url), original(url))[1], raising=False
    )

    import mf_faq.ingest.fetch as fetch_module

    monkeypatch.setattr(fetch_module, "build_fetcher", lambda *a, **kw: fetcher)
    preview.main(["--live"])
    capsys.readouterr()

    assert len(calls) == len(ALL_SCHEMES) == len(set(calls))


# -- read-only guarantee ---------------------------------------------------


def test_preview_never_touches_the_registry(monkeypatch, capsys, no_network):
    """The module docstring promises it is strictly read-only. Nothing in it may
    open the registry — the reason it is safe to run against a live index."""
    import sqlite3

    def forbidden(*args, **kwargs):
        raise AssertionError("preview opened the registry")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    assert preview.main([]) == 0
    capsys.readouterr()


def test_preview_module_does_not_import_the_registry_writers():
    """A structural guard, not a behavioural one: change.py and pipeline.py are
    the only code that writes documents. Importing either here would make the
    read-only promise depend on nobody calling them."""
    source = pathlib.Path(preview.__file__).read_text(encoding="utf-8")
    assert "ingest.change" not in source
    assert "ingest.pipeline" not in source
    assert "mf_faq.db" not in source
