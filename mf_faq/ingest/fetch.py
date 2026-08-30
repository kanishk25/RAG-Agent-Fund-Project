"""Polite HTTP fetching for the ingestion pipeline (P2.1, P2.2).

Four properties this module exists to guarantee:

1. **Two separate allowlists** (ARCH §12). `fetch()` reaches only the fact
   domain; `check_link()` reaches only the refusal-link domains and returns a
   status code, never a body. They are different methods with different return
   types precisely so a link-health domain cannot become a fact source by
   accident — the "fallback corpus" PS §8.1 removed.

2. **robots.txt is re-read every run** (edge case I-08). A `Fetcher` instance is
   one run's scope, and its robots cache dies with it. Permission granted in
   January is not permission in June.

3. **One fetch per URL per run** (ARCH §14). A Groww page yields seven facts;
   fetching it seven times would be seven times the load for identical bytes.

4. **A 200 is not automatically a success** (edge cases I-01/I-02). A bot-block
   page, a captcha interstitial, or an empty body all return 200. Treating them
   as successful fetches would hand the parser garbage and, worse, could look
   like "the page no longer contains NAV" rather than "we were blocked".
"""

from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from mf_faq.logging_setup import get_logger

log = get_logger(__name__)

# A real Groww scheme page is ~400 KB. Anything trivially small is an error
# page, a redirect stub, or a block — not content (I-01/I-02).
MIN_PLAUSIBLE_BODY_BYTES = 2_048


class FetchError(RuntimeError):
    """Base for every fetch failure. All are run-failing (ARCH §11)."""


class NotAllowlisted(FetchError):
    """URL is outside the permitted domain for this call. PS §5.1 / ARCH §12."""


class DisallowedByRobots(FetchError):
    """robots.txt forbids this path. PS §4.5 behaviour 8 — non-negotiable."""


class HttpFetchError(FetchError):
    """Non-2xx, network failure, or exhausted retries."""


class SuspiciousContent(FetchError):
    """HTTP 200 whose body is implausible as real content (I-01/I-02)."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status_code: int
    body: str
    fetched_at: str
    elapsed_s: float
    from_cache: bool = False

    @property
    def was_redirected(self) -> bool:
        """True if the server sent us somewhere else.

        The parser must check scheme identity when this is set: a silent
        redirect would ingest the wrong fund's data under the right scheme_id
        (edge case I-06, high severity).
        """
        return self.final_url.rstrip("/") != self.url.rstrip("/")


@dataclass
class FetchStats:
    requests_made: int = 0
    cache_hits: int = 0
    retries: int = 0
    sleep_seconds: float = 0.0
    hosts: set[str] = field(default_factory=set)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


class Fetcher:
    """One instance == one ingestion run.

    Scoping it to a run is what makes the robots re-check and the URL
    de-duplication correct: both caches must not outlive the run.
    """

    def __init__(
        self,
        *,
        fact_domain: str,
        user_agent: str,
        crawl_delay_seconds: float = 3.0,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        link_health_domains: tuple[str, ...] = (),
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self.fact_domain = fact_domain.removeprefix("www.")
        self.user_agent = user_agent
        self.crawl_delay_seconds = crawl_delay_seconds
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.link_health_domains = frozenset(d.removeprefix("www.") for d in link_health_domains)

        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )

        self._page_cache: dict[str, FetchResult] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_request_at: dict[str, float] = {}
        self.stats = FetchStats()

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- politeness --------------------------------------------------------

    def _robots_for(self, host: str) -> urllib.robotparser.RobotFileParser | None:
        """Fetch and parse robots.txt once per host per run.

        A robots.txt we cannot retrieve returns None, which is treated as
        *permissive*. That is the conventional reading, but it is a deliberate
        choice worth knowing about: an outage at the robots endpoint does not
        halt ingestion.
        """
        if host in self._robots:
            return self._robots[host]

        url = f"https://{host}/robots.txt"
        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            response = self._client.get(url, headers={"User-Agent": self.user_agent})
            if response.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
            else:
                log.warning(
                    "robots.txt not 200", extra={"host": host, "status": response.status_code}
                )
        except httpx.HTTPError as exc:
            log.warning("robots.txt unreachable", extra={"host": host, "error": str(exc)})

        self._robots[host] = parser
        return parser

    def _assert_robots_allows(self, url: str) -> None:
        parser = self._robots_for(_host(url))
        if parser is None:
            return
        if not parser.can_fetch(self.user_agent, url):
            raise DisallowedByRobots(
                f"robots.txt disallows {url} for '{self.user_agent}'. "
                "PS §4.5 behaviour 8 makes this non-negotiable — do not override."
            )

    def _effective_delay(self, host: str) -> float:
        """Honour robots.txt Crawl-delay when it exceeds our configured delay.

        groww.in publishes none today, so the config value applies. If one
        appears later, the site's wishes win.
        """
        parser = self._robots.get(host)
        declared = None
        if parser is not None:
            try:
                declared = parser.crawl_delay(self.user_agent)
            except (AttributeError, ValueError):
                declared = None
        if declared is None:
            return self.crawl_delay_seconds
        return max(self.crawl_delay_seconds, float(declared))

    def _throttle(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is not None:
            wait = self._effective_delay(host) - (time.monotonic() - last)
            if wait > 0:
                self.stats.sleep_seconds += wait
                self._sleep(wait)
        self._last_request_at[host] = time.monotonic()

    # -- fact fetching -----------------------------------------------------

    def fetch(self, url: str) -> FetchResult:
        """Fetch a fact page. Restricted to the fact domain.

        De-duplicated per run: the second caller for the same URL gets the
        cached body without a second request.
        """
        host = _host(url)
        if host != self.fact_domain:
            raise NotAllowlisted(
                f"'{host}' is not the fact domain '{self.fact_domain}'. "
                "Only Groww may be fetched for facts (PS §5.1) — AMC, AMFI and "
                "SEBI are refusal-link targets, not sources."
            )

        if url in self._page_cache:
            self.stats.cache_hits += 1
            cached = self._page_cache[url]
            log.debug("fetch cache hit", extra={"url": url})
            return FetchResult(**{**cached.__dict__, "from_cache": True})

        self._assert_robots_allows(url)
        result = self._request(url)
        self._page_cache[url] = result
        return result

    def _request(self, url: str) -> FetchResult:
        host = _host(url)
        self.stats.hosts.add(host)
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            if attempt:
                self.stats.retries += 1
                backoff = 2.0**attempt
                log.warning(
                    "fetch retry", extra={"url": url, "attempt": attempt, "backoff_s": backoff}
                )
                self._sleep(backoff)

            self._throttle(host)
            started = time.monotonic()
            try:
                response = self._client.get(url, headers={"User-Agent": self.user_agent})
            except httpx.HTTPError as exc:
                last_error = exc
                continue

            self.stats.requests_made += 1
            elapsed = time.monotonic() - started

            if response.status_code == 429:
                # Being rate-limited by Groww is a real operating signal, not a
                # transient blip: recurring 429s mean the crawl policy is wrong
                # and PS §8.1 needs revisiting (edge case I-07).
                retry_after = response.headers.get("retry-after")
                last_error = HttpFetchError(f"429 rate limited (retry-after={retry_after})")
                if retry_after and retry_after.isdigit():
                    self._sleep(float(retry_after))
                continue

            if response.status_code >= 500:
                last_error = HttpFetchError(f"server error {response.status_code}")
                continue

            if response.status_code >= 400:
                # 4xx is not retryable — a 404 means the scheme page moved or the
                # fund was merged (I-05). Retrying wastes politeness budget.
                raise HttpFetchError(f"{response.status_code} for {url} (not retryable)")

            body = response.text
            if len(body.encode("utf-8")) < MIN_PLAUSIBLE_BODY_BYTES:
                raise SuspiciousContent(
                    f"{url} returned {response.status_code} but only "
                    f"{len(body)} chars — likely a block page or empty shell, not content"
                )

            final_url = str(response.url)
            if final_url.rstrip("/") != url.rstrip("/"):
                # Not fatal here — the parser verifies scheme identity (I-06) —
                # but it must be visible in the run log when it happens.
                log.warning("redirected", extra={"url": url, "final_url": final_url})

            return FetchResult(
                url=url,
                final_url=final_url,
                status_code=response.status_code,
                body=body,
                fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
                elapsed_s=round(elapsed, 3),
            )

        raise HttpFetchError(f"failed after {self.max_retries + 1} attempts: {url}") from last_error

    # -- link health (never a fact source) ---------------------------------

    def check_link(self, url: str) -> int:
        """HEAD a refusal link and return its status code. Never returns a body.

        Refusal responses are a *required* output path (PS §4.3), so a dead
        educational or factsheet link is a broken feature, not cosmetic
        (ARCH §8.5). Returning only an int is the structural guarantee that
        this method cannot be repurposed to source facts.
        """
        host = _host(url)
        if host not in self.link_health_domains:
            raise NotAllowlisted(
                f"'{host}' is not a link-health domain {sorted(self.link_health_domains)}"
            )

        self._throttle(host)
        self.stats.hosts.add(host)
        try:
            response = self._client.head(
                url, headers={"User-Agent": self.user_agent}, follow_redirects=True
            )
            self.stats.requests_made += 1
        except httpx.HTTPError as exc:
            log.warning("link health check failed", extra={"url": url, "error": str(exc)})
            return 0
        return response.status_code


def build_fetcher(sources, refusal_links=None, **overrides) -> Fetcher:
    """Construct a Fetcher from validated config (P1 schemas)."""
    fs = sources.fact_source
    link_domains = tuple(refusal_links.link_health_domains) if refusal_links else ()
    kwargs = {
        "fact_domain": fs.domain,
        "user_agent": fs.user_agent,
        "crawl_delay_seconds": fs.crawl_delay_seconds,
        "timeout_seconds": fs.timeout_seconds,
        "max_retries": fs.max_retries,
        "link_health_domains": link_domains,
    }
    kwargs.update(overrides)
    return Fetcher(**kwargs)
