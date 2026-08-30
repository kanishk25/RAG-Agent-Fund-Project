"""Fetcher tests (P2.1, P2.2).

Every test runs against httpx.MockTransport — no network. Per ARCH §15.7 the
parser and fetcher must never depend on the live site, or the suite breaks
whenever Groww has a bad day.
"""

from __future__ import annotations

import httpx
import pytest

from mf_faq.ingest.fetch import (
    DisallowedByRobots,
    Fetcher,
    HttpFetchError,
    NotAllowlisted,
    SuspiciousContent,
    build_fetcher,
)
from mf_faq.settings import get_refusal_links, get_sources

PAGE = "https://groww.in/mutual-funds/motilal-oswal-large-and-midcap-fund-direct-growth"
BODY = "<html><body>" + ("x" * 5_000) + "</body></html>"

ROBOTS_PERMISSIVE = """User-agent: *
Disallow: /dashboard/
Disallow: /mutual-funds/filter?*
Disallow: /v1/api/*
"""

ROBOTS_BLOCKS_SCHEMES = """User-agent: *
Disallow: /mutual-funds/
"""


def make_fetcher(handler, *, robots: str = ROBOTS_PERMISSIVE, **kwargs) -> Fetcher:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(route), follow_redirects=True)
    defaults = {
        "fact_domain": "groww.in",
        "user_agent": "mf-faq-bot/1.0 (+https://example.test/repo)",
        "crawl_delay_seconds": 3.0,
        "link_health_domains": ("motilaloswalmf.com",),
        "client": client,
        "sleep": lambda _s: None,  # never actually sleep in tests
    }
    return Fetcher(**{**defaults, **kwargs})


# -- allowlists (P2.2) -----------------------------------------------------


def test_fetches_from_the_fact_domain():
    f = make_fetcher(lambda r: httpx.Response(200, text=BODY))
    assert f.fetch(PAGE).status_code == 200


@pytest.mark.parametrize(
    "url",
    [
        "https://www.amfiindia.com/spages/NAVAll.txt",
        "https://www.sebi.gov.in/investors.html",
        "https://www.motilaloswalmf.com/mutual-funds/motilal-oswal-elss-tax-saver-fund",
        "https://example.com/anything",
    ],
)
def test_blocks_every_non_fact_domain(url):
    """Exit criterion: AMFI and SEBI must be blocked on the ingestion path.

    motilaloswalmf.com is included deliberately — it IS allowlisted for link
    health, and must still be refused as a fact source (ARCH §12).
    """
    f = make_fetcher(lambda r: httpx.Response(200, text=BODY))
    with pytest.raises(NotAllowlisted):
        f.fetch(url)


def test_link_health_allows_only_its_own_domains():
    f = make_fetcher(lambda r: httpx.Response(200))
    assert f.check_link("https://www.motilaloswalmf.com/investor-education") == 200
    with pytest.raises(NotAllowlisted):
        f.check_link(PAGE)


def test_link_health_returns_a_status_not_a_body():
    """Structural guarantee: check_link cannot be repurposed to source facts."""
    f = make_fetcher(lambda r: httpx.Response(200, text="secret content"))
    result = f.check_link("https://www.motilaloswalmf.com/investor-education")
    assert result == 200
    assert isinstance(result, int)


def test_link_health_reports_dead_link():
    """A dead refusal link is a broken feature (PS §4.3), so it must surface."""
    f = make_fetcher(lambda r: httpx.Response(404))
    assert f.check_link("https://www.motilaloswalmf.com/gone") == 404


def test_link_health_network_error_returns_zero_not_raise():
    """The daily run must not die because an outbound link timed out."""

    def boom(request):
        raise httpx.ConnectError("unreachable")

    f = make_fetcher(boom)
    assert f.check_link("https://www.motilaloswalmf.com/investor-education") == 0


# -- robots.txt (I-08) -----------------------------------------------------


def test_respects_robots_disallow():
    f = make_fetcher(lambda r: httpx.Response(200, text=BODY), robots=ROBOTS_BLOCKS_SCHEMES)
    with pytest.raises(DisallowedByRobots, match="non-negotiable"):
        f.fetch(PAGE)


def test_robots_is_refetched_each_run():
    """A new Fetcher is a new run: permission is re-checked, never assumed."""
    calls = {"robots": 0}

    def route(request):
        if request.url.path == "/robots.txt":
            calls["robots"] += 1
            return httpx.Response(200, text=ROBOTS_PERMISSIVE)
        return httpx.Response(200, text=BODY)

    for _ in range(2):
        client = httpx.Client(transport=httpx.MockTransport(route))
        Fetcher(
            fact_domain="groww.in",
            user_agent="mf-faq-bot/1.0 (+https://example.test)",
            client=client,
            sleep=lambda _s: None,
        ).fetch(PAGE)

    assert calls["robots"] == 2


def test_robots_fetched_once_within_a_run():
    calls = {"robots": 0}

    def route(request):
        if request.url.path == "/robots.txt":
            calls["robots"] += 1
            return httpx.Response(200, text=ROBOTS_PERMISSIVE)
        return httpx.Response(200, text=BODY)

    client = httpx.Client(transport=httpx.MockTransport(route))
    f = Fetcher(
        fact_domain="groww.in",
        user_agent="mf-faq-bot/1.0 (+https://example.test)",
        client=client,
        sleep=lambda _s: None,
    )
    f.fetch(PAGE)
    f.fetch(PAGE + "-other")
    assert calls["robots"] == 1


def test_unreachable_robots_is_treated_as_permissive():
    def route(request):
        if request.url.path == "/robots.txt":
            raise httpx.ConnectError("down")
        return httpx.Response(200, text=BODY)

    client = httpx.Client(transport=httpx.MockTransport(route))
    f = Fetcher(
        fact_domain="groww.in",
        user_agent="mf-faq-bot/1.0 (+https://example.test)",
        client=client,
        sleep=lambda _s: None,
    )
    assert f.fetch(PAGE).status_code == 200


def test_honours_robots_crawl_delay_when_longer_than_config():
    slept: list[float] = []
    robots = ROBOTS_PERMISSIVE + "Crawl-delay: 10\n"
    f = make_fetcher(
        lambda r: httpx.Response(200, text=BODY),
        robots=robots,
        crawl_delay_seconds=3.0,
        sleep=slept.append,
    )
    f.fetch(PAGE)
    f.fetch(PAGE + "-two")
    # The wait is (delay - time already elapsed), so it lands just under 10s.
    # What matters is that robots' 10s won over the configured 3s.
    assert slept and max(slept) > 9.0


# -- de-duplication (ARCH §14) --------------------------------------------


def test_one_fetch_per_url_per_run():
    """Exit criterion: a page serving 7 doc_types is fetched once, not 7 times."""
    calls = {"page": 0}

    def handler(request):
        calls["page"] += 1
        return httpx.Response(200, text=BODY)

    f = make_fetcher(handler)
    for _ in range(7):
        f.fetch(PAGE)

    assert calls["page"] == 1
    assert f.stats.requests_made == 1
    assert f.stats.cache_hits == 6


def test_cached_result_is_flagged_and_identical():
    f = make_fetcher(lambda r: httpx.Response(200, text=BODY))
    first = f.fetch(PAGE)
    second = f.fetch(PAGE)
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.body == first.body


def test_distinct_urls_are_fetched_separately():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text=BODY)

    f = make_fetcher(handler)
    f.fetch(PAGE)
    f.fetch(PAGE + "-two")
    assert calls["n"] == 2


# -- rate limiting ---------------------------------------------------------


def test_throttles_between_requests_but_not_before_the_first():
    slept: list[float] = []
    f = make_fetcher(lambda r: httpx.Response(200, text=BODY), sleep=slept.append)
    f.fetch(PAGE)
    assert slept == []  # no delay before the first request
    f.fetch(PAGE + "-two")
    assert len(slept) == 1 and 0 < slept[0] <= 3.0


# -- retries and errors ----------------------------------------------------


def test_retries_5xx_then_succeeds():
    seq = [500, 503, 200]

    def handler(request):
        status = seq.pop(0)
        return httpx.Response(status, text=BODY if status == 200 else "err")

    f = make_fetcher(handler, max_retries=2)
    assert f.fetch(PAGE).status_code == 200
    assert f.stats.retries == 2


def test_gives_up_after_max_retries():
    f = make_fetcher(lambda r: httpx.Response(503, text="down"), max_retries=1)
    with pytest.raises(HttpFetchError, match="failed after 2 attempts"):
        f.fetch(PAGE)


def test_does_not_retry_4xx():
    """A 404 means the scheme page moved or the fund merged (I-05) — not transient."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, text="gone")

    f = make_fetcher(handler, max_retries=3)
    with pytest.raises(HttpFetchError, match="not retryable"):
        f.fetch(PAGE)
    assert calls["n"] == 1


def test_429_is_retried_and_honours_retry_after():
    """Being rate-limited by Groww is a real signal (I-07), not a blip."""
    slept: list[float] = []
    seq = [429, 200]

    def handler(request):
        status = seq.pop(0)
        if status == 429:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(200, text=BODY)

    f = make_fetcher(handler, max_retries=2, sleep=slept.append)
    assert f.fetch(PAGE).status_code == 200
    assert 7.0 in slept


def test_network_error_is_retried():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectTimeout("timeout")
        return httpx.Response(200, text=BODY)

    f = make_fetcher(handler, max_retries=2)
    assert f.fetch(PAGE).status_code == 200


# -- content sanity (I-01 / I-02) -----------------------------------------


def test_rejects_200_with_empty_body():
    f = make_fetcher(lambda r: httpx.Response(200, text=""))
    with pytest.raises(SuspiciousContent):
        f.fetch(PAGE)


def test_rejects_200_with_tiny_body():
    """A bot-block or captcha page returns 200 — it must not count as success."""
    f = make_fetcher(lambda r: httpx.Response(200, text="<html>Access denied</html>"))
    with pytest.raises(SuspiciousContent, match="block page or empty shell"):
        f.fetch(PAGE)


# -- redirects (I-06) ------------------------------------------------------


def test_redirect_is_recorded_for_the_parser_to_check():
    """A silent redirect would ingest the wrong fund under the right scheme_id."""
    other = "https://groww.in/mutual-funds/some-other-fund-direct-growth"

    def handler(request):
        if request.url.path.endswith("large-and-midcap-fund-direct-growth"):
            return httpx.Response(302, headers={"location": other})
        return httpx.Response(200, text=BODY)

    result = make_fetcher(handler).fetch(PAGE)
    assert result.was_redirected is True
    assert result.final_url == other


def test_no_redirect_reports_false():
    assert (
        make_fetcher(lambda r: httpx.Response(200, text=BODY)).fetch(PAGE).was_redirected is False
    )


# -- identity and construction --------------------------------------------


def test_sends_the_identifying_user_agent():
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, text=BODY)

    make_fetcher(handler).fetch(PAGE)
    assert seen and seen[0].startswith("mf-faq-bot/")


def test_build_fetcher_uses_validated_config():
    """Wiring check: the real configs produce a correctly-restricted fetcher."""
    f = build_fetcher(get_sources(), get_refusal_links(), client=httpx.Client())
    try:
        assert f.fact_domain == "groww.in"
        assert "motilaloswalmf.com" in f.link_health_domains
        assert f.crawl_delay_seconds == 3.0
        assert f.user_agent.startswith("mf-faq-bot/")
    finally:
        f.close()


def test_fetch_result_records_both_timestamps_material():
    """fetched_at is recorded here; source_as_of comes from the document (PS §8.3)."""
    result = make_fetcher(lambda r: httpx.Response(200, text=BODY)).fetch(PAGE)
    assert result.fetched_at.startswith("20")
    assert result.elapsed_s >= 0
