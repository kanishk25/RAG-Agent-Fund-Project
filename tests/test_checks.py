"""Missing-update alerting tests (P6.6, ARCH §8.5). Offline throughout —
link-health tests drive `httpx.MockTransport`, matching test_fetch.py."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from mf_faq import db
from mf_faq.guardrails.freshness import IST
from mf_faq.ingest.checks import (
    Alert,
    check_consecutive_failures,
    check_doc_type_freshness,
    check_link_health,
    main,
    run_all_checks,
)
from mf_faq.ingest.fetch import Fetcher
from mf_faq.settings import get_refusal_links, get_sources


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=IST)


def _insert_document(db_path, *, scheme_id, doc_type, source_as_of, status="ok"):
    with db.session(db_path) as conn:
        conn.execute(
            "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, content_hash, "
            "card_hash, source_as_of, fetched_at, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                f"{scheme_id}:{doc_type}",
                scheme_id,
                doc_type,
                "https://groww.in/mutual-funds/x",
                "hash",
                "cardhash",
                source_as_of,
                "2026-08-30T12:00:00+05:30",
                status,
            ),
        )


def _insert_run(db_path, run_id, status, started_at="2026-08-30T06:30:00Z"):
    with db.session(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, finished_at, status, "
            "sources_attempted, sources_changed, sources_failed) VALUES (?,?,?,?,?,?,?)",
            (run_id, started_at, started_at, status, 35, 0, 0 if status == "success" else 35),
        )


@pytest.fixture
def fresh_db(tmp_path):
    path = tmp_path / "registry.db"
    db.init_db(path)
    return path


@pytest.fixture
def sources():
    return get_sources()


@pytest.fixture
def refusal_links():
    return get_refusal_links()


# -- NAV / holdings freshness -----------------------------------------------


class TestDocTypeFreshness:
    def test_fresh_nav_raises_no_alert(self, fresh_db, sources):
        _insert_document(fresh_db, scheme_id="mo_elss", doc_type="nav", source_as_of="2026-08-28")
        now = _at("2026-08-28T18:00:00")  # same day
        alerts = check_doc_type_freshness(fresh_db, "nav", now=now, sources=sources)
        assert alerts == []

    def test_stale_nav_alerts(self, fresh_db, sources):
        _insert_document(fresh_db, scheme_id="mo_elss", doc_type="nav", source_as_of="2026-08-27")
        # Thursday -> Monday is 2 business days elapsed, over the 1-day floor.
        now = _at("2026-08-31T09:00:00")
        alerts = check_doc_type_freshness(fresh_db, "nav", now=now, sources=sources)
        assert len(alerts) == 1
        assert alerts[0].check == "nav_stale"
        assert "mo_elss" in alerts[0].detail

    def test_alerts_once_per_scheme_not_once_overall(self, fresh_db, sources):
        for scheme_id in ("mo_elss", "mo_next50"):
            _insert_document(
                fresh_db, scheme_id=scheme_id, doc_type="nav", source_as_of="2026-08-20"
            )
        now = _at("2026-08-31T09:00:00")
        alerts = check_doc_type_freshness(fresh_db, "nav", now=now, sources=sources)
        assert {a.detail.split(":")[0] for a in alerts} == {"mo_elss", "mo_next50"}

    def test_holdings_flag_verdict_still_alerts(self, fresh_db, sources):
        """Holdings is FLAG policy, not REFUSE — the ops alert must not wait
        for a REFUSE verdict, or weeks of degraded answers pass unnoticed."""
        _insert_document(
            fresh_db, scheme_id="mo_elss", doc_type="holdings", source_as_of="2026-07-01"
        )
        now = _at("2026-08-31T09:00:00")  # well past the 45-day flag threshold
        alerts = check_doc_type_freshness(fresh_db, "holdings", now=now, sources=sources)
        assert len(alerts) == 1
        assert "verdict=flag" in alerts[0].detail

    def test_empty_registry_raises_no_alert(self, fresh_db, sources):
        assert check_doc_type_freshness(fresh_db, "nav", now=_at("2026-08-31T09:00:00")) == []


# -- consecutive failures -----------------------------------------------


class TestConsecutiveFailures:
    def test_below_threshold_no_alert(self, fresh_db):
        _insert_run(fresh_db, "r1", "failed")
        _insert_run(fresh_db, "r2", "failed")
        assert check_consecutive_failures(fresh_db, threshold=3) == []

    def test_three_failures_alerts(self, fresh_db):
        for i in range(3):
            _insert_run(fresh_db, f"r{i}", "failed")
        alerts = check_consecutive_failures(fresh_db, threshold=3)
        assert len(alerts) == 1
        assert alerts[0].check == "consecutive_failures"

    def test_a_success_in_the_window_clears_it(self, fresh_db):
        _insert_run(fresh_db, "r1", "failed")
        _insert_run(fresh_db, "r2", "success")
        _insert_run(fresh_db, "r3", "failed")
        assert check_consecutive_failures(fresh_db, threshold=3) == []

    def test_empty_registry_no_alert(self, fresh_db):
        assert check_consecutive_failures(fresh_db, threshold=3) == []


# -- link health -----------------------------------------------------------


def _make_fetcher(handler, link_health_domains=("motilaloswalmf.com",)) -> Fetcher:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\n")
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(route), follow_redirects=True)
    return Fetcher(
        fact_domain="groww.in",
        user_agent="mf-faq-bot/1.0 (+https://example.test/repo)",
        crawl_delay_seconds=0.0,
        link_health_domains=link_health_domains,
        client=client,
        sleep=lambda _s: None,
    )


class TestLinkHealth:
    def test_all_200_raises_no_alerts(self, refusal_links):
        fetcher = _make_fetcher(lambda r: httpx.Response(200))
        assert check_link_health(fetcher, refusal_links) == []

    def test_one_dead_link_alerts(self, refusal_links):
        def route(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/investor-education":
                return httpx.Response(404)
            return httpx.Response(200)

        fetcher = _make_fetcher(route)
        alerts = check_link_health(fetcher, refusal_links)
        assert len(alerts) >= 1
        assert all(a.check == "link_unhealthy" for a in alerts)
        assert any("investor-education" in a.detail for a in alerts)

    def test_unreachable_link_alerts_not_raises(self, refusal_links):
        """`check_link` already turns a network failure into status 0 (P2.2) —
        this asserts that contract is what link-health alerting relies on."""

        def boom(request):
            raise httpx.ConnectError("unreachable")

        fetcher = _make_fetcher(boom)
        alerts = check_link_health(fetcher, refusal_links)
        assert len(alerts) > 0
        assert all("HTTP 0" in a.detail for a in alerts)


# -- run_all_checks integration ---------------------------------------------


class TestRunAllChecks:
    def test_clean_registry_and_healthy_links_is_silent(self, fresh_db, sources, refusal_links):
        _insert_document(fresh_db, scheme_id="mo_elss", doc_type="nav", source_as_of="2026-08-28")
        fetcher = _make_fetcher(lambda r: httpx.Response(200))
        alerts = run_all_checks(
            fresh_db,
            fetcher=fetcher,
            sources=sources,
            refusal_links=refusal_links,
            now=_at("2026-08-28T18:00:00"),
        )
        assert alerts == []

    def test_skip_link_health_avoids_needing_a_fetcher(self, fresh_db, sources, refusal_links):
        """No `fetcher=` passed and no live network available in tests — this
        must not attempt to build a real one when link health is skipped."""
        alerts = run_all_checks(
            fresh_db,
            sources=sources,
            refusal_links=refusal_links,
            now=_at("2026-08-28T18:00:00"),
            skip_link_health=True,
        )
        assert alerts == []

    def test_combines_alerts_from_every_check(self, fresh_db, sources, refusal_links):
        _insert_document(fresh_db, scheme_id="mo_elss", doc_type="nav", source_as_of="2026-07-01")
        for i in range(3):
            _insert_run(fresh_db, f"r{i}", "failed")
        alerts = run_all_checks(
            fresh_db,
            sources=sources,
            refusal_links=refusal_links,
            now=_at("2026-08-31T09:00:00"),
            skip_link_health=True,
        )
        checks_seen = {a.check for a in alerts}
        assert "nav_stale" in checks_seen
        assert "consecutive_failures" in checks_seen


# -- CLI ---------------------------------------------------------------


class TestCli:
    def test_alerts_exit_nonzero_and_print_json(self, fresh_db, capsys):
        for i in range(3):
            _insert_run(fresh_db, f"r{i}", "failed")
        code = main(["--db", str(fresh_db), "--skip-link-health", "--json"])
        assert code == 1
        out = capsys.readouterr().out
        assert "consecutive_failures" in out

    def test_empty_registry_exits_zero(self, fresh_db, capsys):
        code = main(["--db", str(fresh_db), "--skip-link-health"])
        assert code == 0
        assert "no alerts" in capsys.readouterr().out


def test_alert_is_a_plain_frozen_dataclass():
    a = Alert(check="x", detail="y")
    assert a.check == "x"
    assert a.detail == "y"
    with pytest.raises(AttributeError):
        a.check = "z"  # type: ignore[misc]
