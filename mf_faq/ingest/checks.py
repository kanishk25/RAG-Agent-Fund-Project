"""Missing-update alerting (P6.6, ARCH §8.5).

Every source is fetched daily, so *absence* of an expected change is itself a
signal (PS §4.5 behaviour 6) — a page that silently stopped updating looks
identical to one that is fine, right up until a user gets a wrong answer
served under a footer date nobody re-checked. This module is what looks.

It runs as its own step (`python -m mf_faq.ingest.checks`), **after** a
`mf-faq-ingest` run and **before** the workflow's commit step — not fused into
`ingest/cli.py`'s exit code, on purpose: `cli.py`'s test suite drives an
offline `httpx.MockTransport` fixture with no route for the link-health
domain, and grafting a live-network check onto every one of those tests would
be needless churn for a check that only actually needs to run once a day.
Keeping it a separate module means the two exit codes compose (either step
failing skips the commit) without either test suite knowing the other exists.

Four checks, matching ARCH §8.5's table exactly:

| Check | Alert when |
|---|---|
| NAV | no fresh `source_as_of` on a trading day |
| Holdings | no fresh `source_as_of` in 45 days |
| Any run | 3 consecutive failed runs |
| Educational/factsheet links | any non-200 HEAD |

**The NAV and holdings checks reuse `guardrails.freshness.evaluate_freshness`
rather than re-deriving staleness rules.** That function already encodes the
exact business-day-aware, IST-pinned policy from `sources.yaml` that a live
query is gated on (P4.3) — a REFUSE or FLAG verdict on a *committed* registry
row means a real query would right now get a stale or refused answer for that
fact, which is precisely what this alert exists to catch before a user does.

**The consecutive-failure check is whole-run, not per-scheme — an honest,
named limitation, not an oversight.** ARCH §8.5 says "any source", which
suggests per-scheme granularity. But today's pipeline (`ingest/pipeline.py`,
the standing S-03 rule) fails the *entire* run the moment any one scheme
fails — there is no partial-success registry write to look back on, so a
single flaky scheme already turns today's run red and raises an issue via
`cli.py`'s own exit code, before this check would ever see a second failure
to count. What this check adds is the coarser, still-real signal ARCH's
silent-disable risk (§15.6) is actually worried about: ingestion has been
broken for `threshold` days running, not just today.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mf_faq.guardrails.freshness import FreshnessVerdict, evaluate_freshness
from mf_faq.ingest.fetch import Fetcher, build_fetcher
from mf_faq.logging_setup import configure_logging, get_logger
from mf_faq.schemas import RefusalLinksConfig, SourcesConfig
from mf_faq.settings import get_refusal_links, get_settings, get_sources

log = get_logger(__name__)

CONSECUTIVE_FAILURE_THRESHOLD = 3

#: `doc_types` ARCH §8.5 names for freshness alerting. Not every `doc_type` in
#: `sources.yaml` is here — e.g. `expense_ratio`/`exit_load` are FLAG-policy
#: too, but ARCH's table calls out only NAV and holdings for this particular
#: ops alert, leaving the rest to the per-query freshness gate alone.
_FRESHNESS_CHECKED_DOC_TYPES = ("nav", "holdings")


@dataclass(frozen=True)
class Alert:
    """One tripped check. `check` is a stable machine-readable slug; `detail`
    is the human-readable line an operator reads first (mirrors
    `pipeline._error_detail`'s shape)."""

    check: str
    detail: str


def check_doc_type_freshness(
    db_path: Path,
    doc_type: str,
    *,
    now: datetime | None = None,
    sources: SourcesConfig | None = None,
) -> list[Alert]:
    """Alert on every registry row of `doc_type` that is not FRESH.

    Imported here, not at module scope: `mf_faq.db` has no heavy deps, but
    keeping this import local to the function that uses it matches the rest
    of the ingest package's habit of not paying for anything a caller (e.g. a
    `--json`-only report reader) doesn't need.
    """
    from mf_faq import db

    sources = sources or get_sources()
    alerts: list[Alert] = []
    for row in db.documents_by_type(db_path, doc_type):
        source_as_of = datetime.fromisoformat(row["source_as_of"]).date()
        check = evaluate_freshness(doc_type, source_as_of, now=now, sources=sources)
        if check.verdict is FreshnessVerdict.FRESH:
            continue
        alerts.append(
            Alert(
                check=f"{doc_type}_stale",
                detail=(
                    f"{row['scheme_id']}:{doc_type} source_as_of={row['source_as_of']} "
                    f"age={check.age} {check.unit} verdict={check.verdict.value}"
                ),
            )
        )
    return alerts


def check_consecutive_failures(
    db_path: Path, *, threshold: int = CONSECUTIVE_FAILURE_THRESHOLD
) -> list[Alert]:
    """See the module docstring for why this is whole-run, not per-scheme."""
    from mf_faq import db

    runs = db.recent_runs(db_path, threshold)
    if len(runs) < threshold or any(r["status"] != "failed" for r in runs):
        return []
    return [
        Alert(
            check="consecutive_failures",
            detail=f"last {threshold} runs all failed (most recent: {runs[0]['run_id']})",
        )
    ]


def check_link_health(fetcher: Fetcher, refusal_links: RefusalLinksConfig) -> list[Alert]:
    """HEAD every refusal-carried URL. PS §4.3 makes a refusal's link a
    required output, so a dead one is a broken feature, not cosmetic
    (ARCH §8.5) — this is the only place that fact gets checked.

    No try/except here: `Fetcher.check_link` already turns a network failure
    into a `0` status rather than raising (P2.2's own contract — "the daily
    run must not die because an outbound link timed out"), so `!= 200` alone
    catches both a dead link and an unreachable one. A `NotAllowlisted` is
    left to propagate — every URL passed in comes from `refusal_links`
    itself, whose schema validator already guarantees every host is on
    `link_health_domains` (`schemas.py::RefusalLinksConfig._validate_routing`),
    so that exception firing here would mean the config validator has a bug,
    not that a link is unhealthy — a fundamentally different failure that
    should not be laundered into an ordinary alert.
    """
    urls = {str(t.url) for t in refusal_links.factsheet_links.values()}
    urls.add(str(refusal_links.factsheet_fallback.url))
    urls.update(str(t.url) for t in refusal_links.educational_links.values())

    alerts: list[Alert] = []
    for url in sorted(urls):
        status = fetcher.check_link(url)
        if status != 200:
            alerts.append(Alert("link_unhealthy", f"{url}: HTTP {status}"))
    return alerts


def run_all_checks(
    db_path: Path,
    *,
    fetcher: Fetcher | None = None,
    sources: SourcesConfig | None = None,
    refusal_links: RefusalLinksConfig | None = None,
    now: datetime | None = None,
    skip_link_health: bool = False,
) -> list[Alert]:
    """Every check in ARCH §8.5's table. `fetcher` is injectable so tests
    drive `check_link_health` offline, matching `ingest.pipeline.run`'s own
    `fetcher` parameter."""
    sources = sources or get_sources()
    refusal_links = refusal_links or get_refusal_links()

    alerts: list[Alert] = []
    for doc_type in _FRESHNESS_CHECKED_DOC_TYPES:
        alerts += check_doc_type_freshness(db_path, doc_type, now=now, sources=sources)
    alerts += check_consecutive_failures(db_path)
    if not skip_link_health:
        fetcher = fetcher or build_fetcher(sources, refusal_links=refusal_links)
        alerts += check_link_health(fetcher, refusal_links)
    return alerts


# --------------------------------------------------------------------------
# CLI — a separate step from `mf-faq-ingest`, run after it (see module docstring)
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mf_faq.ingest.checks",
        description="Missing-update alerting against the committed registry (ARCH §8.5).",
        epilog="Facts-only. No investment advice.",
    )
    parser.add_argument("--db", type=Path, default=None, metavar="PATH", help="registry.db path")
    parser.add_argument(
        "--skip-link-health",
        action="store_true",
        help="skip the live HEAD requests to refusal-link domains",
    )
    parser.add_argument("--json", action="store_true", help="emit alerts as JSON")
    parser.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level or get_settings().log_level)

    db_path = args.db or get_settings().registry_db
    alerts = run_all_checks(db_path, skip_link_health=args.skip_link_health)

    if args.json:
        print(json.dumps([{"check": a.check, "detail": a.detail} for a in alerts], indent=2))
    elif alerts:
        print(f"{len(alerts)} alert(s):", file=sys.stderr)
        for alert in alerts:
            print(f"  [{alert.check}] {alert.detail}", file=sys.stderr)
    else:
        print("no alerts")

    for alert in alerts:
        log.warning("missing-update alert", extra={"check": alert.check, "detail": alert.detail})

    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
