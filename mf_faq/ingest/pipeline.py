"""Orchestrate one full ingestion run (P2.8, ARCH §8.2b).

    fetch → normalise → parse → render cards → compare → write `documents`

Every stage already exists and is tested on its own; this module is about the
seams between them, and three of those seams carry real decisions.

**1. The registry write is all-or-nothing across schemes.**
Cards are collected from every scheme first and reconciled in a single
transaction at the end. If any scheme fails, nothing is written at all — not
even the four schemes that succeeded. That is the local mirror of ARCH §8.4:
a failed run exits non-zero, the workflow's commit step never runs, and the repo
keeps the previous good index. Writing the partial result locally while CI
discards it would make the two environments disagree about what a failed run
leaves behind, which is exactly the kind of divergence that gets discovered at
3am. (This is the standing S-03 rule — "4 of 5 succeed" still fails the run. The
edge case remains open in edge-cases.md; this module implements the current
ruling, it does not settle it.)

**2. The run log is written even when the run fails.**
It goes in its own transactions, before and after the document write, because a
failed run that leaves no evidence of having failed is worse than useless — the
`runs` table is what `GET /freshness` reads and what PS §4.5 behaviour 9 rests
on. Success is a property of the documents write; the log records it either way.

**3. A missing fact is not a failed run, but a missing fact that used to be
there is worth saying out loud.**
PS §5.1 makes an absent or undated fact out of corpus: it refuses at query time,
and failing the whole run over one would freeze the corpus for every other fact
on the page. But the registry knows the difference between "we never had this"
and "we had this yesterday and it is gone today", and only the second is a
regression. Both are counted; neither fails the run. Turning the second into an
alert is P6.6, which is also where the trading-day and 45-day checks belong.

What this module does NOT do: embed anything. Chroma arrives in Phase 3. Each
`Decision` carries `needs_embedding`, and `RunReport.cards_to_embed()` is the
handoff — the pipeline's job ends at a correct registry.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from mf_faq import db
from mf_faq.ingest.change import (
    ChangeSummary,
    Decision,
    detect_changes,
    load_previous,
    reconcile,
    summarise,
)
from mf_faq.ingest.fact_card import FactCard, render_cards
from mf_faq.ingest.fetch import Fetcher, build_fetcher
from mf_faq.ingest.normalise import normalise
from mf_faq.ingest.parse import FactUnavailable, parse_facts
from mf_faq.logging_setup import get_logger
from mf_faq.schemas import SourcesConfig
from mf_faq.settings import get_refusal_links, get_settings, get_sources

log = get_logger(__name__)


@dataclass
class MissingFact:
    """A fact the page did not yield this run."""

    scheme_id: str
    doc_type: str
    reason: str
    #: True if the registry already holds this fact — i.e. we had it and lost
    #: it. A brand-new gap is ordinary coverage (PS §5.1); a regression means
    #: the page changed under us and the stored row will now age untouched.
    regression: bool = False

    @property
    def doc_id(self) -> str:
        return f"{self.scheme_id}:{self.doc_type}"


@dataclass
class SchemeResult:
    """What happened to one scheme page."""

    scheme_id: str
    status: Literal["ok", "failed"]
    requested: int
    cards: list[FactCard] = field(default_factory=list)
    missing: list[MissingFact] = field(default_factory=list)
    error: str | None = None
    source_url: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class RunReport:
    """The full record of one run — what the CLI prints and `runs` stores."""

    run_id: str
    started_at: str
    finished_at: str | None = None
    schemes: list[SchemeResult] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    summary: ChangeSummary = field(default_factory=ChangeSummary)
    committed: bool = False
    dry_run: bool = False
    requests_made: int = 0
    cache_hits: int = 0

    # -- outcome -----------------------------------------------------------

    @property
    def failed_schemes(self) -> list[SchemeResult]:
        return [s for s in self.schemes if not s.ok]

    @property
    def conflicts(self) -> list[Decision]:
        return self.summary.conflicts

    @property
    def missing(self) -> list[MissingFact]:
        return [m for s in self.schemes for m in s.missing]

    @property
    def regressions(self) -> list[MissingFact]:
        return [m for m in self.missing if m.regression]

    @property
    def status(self) -> Literal["success", "failed"]:
        """A run is successful only if every scheme parsed and nothing conflicted.

        Conflicts fail the run even though their rows were written safely (the
        quarantine *is* the write). The point is to turn the Actions run red so
        P6.7 raises an issue — and, because a red run skips the commit step, to
        leave the published index untouched until a human has looked.
        """
        if self.failed_schemes or self.conflicts:
            return "failed"
        return "success"

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "success" else 1

    # -- handoff to Phase 3 ------------------------------------------------

    def cards_to_embed(self) -> list[FactCard]:
        """Cards whose text must be written to the vector index (P3)."""
        return [d.card for d in self.decisions if d.needs_embedding]

    def cards_to_restamp(self) -> list[FactCard]:
        """Cards needing only fresh `source_as_of` metadata — no re-embed."""
        return [d.card for d in self.decisions if d.needs_metadata_update and not d.needs_embedding]

    # -- run log -----------------------------------------------------------

    @property
    def facts_requested(self) -> int:
        return sum(s.requested for s in self.schemes)

    @property
    def facts_failed(self) -> int:
        """Fact-documents that did not land in the registry.

        Keyed on `failed_schemes`, not `committed`, so a dry run reports what a
        real run *would* have counted rather than "everything failed" merely
        because it wrote nothing by design.

        When a scheme fails, the write is skipped for the *whole* corpus, so
        every requested fact failed to land — including the twenty-eight from
        the four schemes that parsed cleanly. Counting only the broken scheme's
        seven would imply the other twenty-eight were refreshed, and the
        registry plainly says they were not.

        Otherwise the failures are the facts the pages did not yield plus the
        updates rejected as conflicts.
        """
        if self.failed_schemes:
            return self.facts_requested
        return len(self.missing) + len(self.conflicts)

    def run_row(self) -> dict[str, int]:
        """Counters for the `runs` table, in fact-documents (ARCH §9).

        A "source" here is a fact-document, not a page: `documents` has one row
        per fact, each with its own `source_as_of` and freshness policy, so
        counting pages would hide six of every seven outcomes.

        Invariant, asserted in the tests: every requested fact lands in exactly
        one bucket. On a run that wrote, that is the five change outcomes plus
        the facts the pages did not yield; on a run that did not, `attempted`
        and `failed` are simply equal.
        """
        return {
            "sources_attempted": self.facts_requested,
            "sources_changed": self.summary.embedded + self.summary.refreshed,
            "sources_failed": self.facts_failed,
        }


# --------------------------------------------------------------------------
# Run-log persistence
# --------------------------------------------------------------------------
# Kept private here rather than in `db.py` so the schema module stays free of
# pipeline semantics. P6.5 (`scheduler/runlog.py`) may lift these out when it
# adds per-run history reporting.


def _new_run_id(started: datetime) -> str:
    """Sortable and collision-free: two runs in the same second must not clash."""
    return f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"


def _open_run(conn: sqlite3.Connection, run_id: str, started_at: str) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, started_at, status) VALUES (?, ?, 'running')",
        (run_id, started_at),
    )


def _close_run(conn: sqlite3.Connection, report: RunReport) -> None:
    counts = report.run_row()
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ?, sources_attempted = ?, "
        "sources_changed = ?, sources_failed = ?, error_detail = ? WHERE run_id = ?",
        (
            report.finished_at,
            report.status,
            counts["sources_attempted"],
            counts["sources_changed"],
            counts["sources_failed"],
            _error_detail(report),
            report.run_id,
        ),
    )


def _error_detail(report: RunReport) -> str | None:
    """One human-readable line per problem — this is what an operator reads first."""
    parts = [f"{s.scheme_id}: {s.error}" for s in report.failed_schemes]
    parts += [f"{d.doc_id}: {d.reason}" for d in report.conflicts]
    return "; ".join(parts) or None


# --------------------------------------------------------------------------
# Per-scheme work
# --------------------------------------------------------------------------


def _ingest_scheme(
    fetcher: Fetcher, sources: SourcesConfig, scheme_id: str, previously_held: set[str]
) -> SchemeResult:
    """Fetch, normalise, parse and render one scheme page.

    Never raises: a scheme failure is data for the report, and the remaining
    schemes still deserve to be attempted so one run surfaces every problem
    rather than only the first.
    """
    scheme = sources.scheme(scheme_id)
    url = str(scheme.url)
    result = SchemeResult(
        scheme_id=scheme_id, status="ok", requested=len(scheme.extract), source_url=url
    )

    try:
        page = fetcher.fetch(url)
        payload = normalise(page.body).payload
        facts, failures = parse_facts(
            payload, scheme_id=scheme_id, source_url=url, doc_types=scheme.extract
        )
    except Exception as exc:  # noqa: BLE001 - deliberate; see docstring
        # Catching broadly is right for a nightly unattended job: one scheme's
        # surprise must not stop the other four from being attempted. Nothing is
        # swallowed — the traceback is logged and the run still exits non-zero.
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        log.exception("scheme failed", extra={"scheme_id": scheme_id, "url": url})
        return result

    result.cards = render_cards(facts, scheme)
    result.missing = [_missing(scheme_id, f, previously_held) for f in failures]

    for gap in result.missing:
        # A gap we have always had is ordinary coverage; one that appeared today
        # means the page changed under us and deserves a louder line.
        log.log(
            logging.WARNING if gap.regression else logging.INFO,
            "fact regressed" if gap.regression else "fact unavailable",
            extra={"scheme_id": scheme_id, "doc_type": gap.doc_type, "reason": gap.reason},
        )

    log.info(
        "scheme ingested",
        extra={
            "scheme_id": scheme_id,
            "facts": len(result.cards),
            "missing": len(result.missing),
            "from_cache": page.from_cache,
        },
    )
    return result


def _missing(scheme_id: str, failure: FactUnavailable, previously_held: set[str]) -> MissingFact:
    doc_id = f"{scheme_id}:{failure.doc_type}"
    return MissingFact(
        scheme_id=scheme_id,
        doc_type=failure.doc_type,
        reason=failure.reason,
        regression=doc_id in previously_held,
    )


def _held_doc_ids(db_path: Path) -> set[str]:
    """Every doc_id the registry already holds — the "did we have this?" baseline."""
    if not db_path.exists():
        return set()
    with contextlib.closing(db.connect(db_path)) as conn:
        return {row["doc_id"] for row in conn.execute("SELECT doc_id FROM documents")}


def _previous_for(db_path: Path, cards: list[FactCard]) -> dict:
    """Registry snapshot for a dry run — read-only, and tolerant of no database."""
    if not db_path.exists():
        return {}
    with contextlib.closing(db.connect(db_path)) as conn:
        return load_previous(conn, [c.doc_id for c in cards])


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def run(
    scheme_ids: list[str] | None = None,
    *,
    db_path: Path | None = None,
    sources: SourcesConfig | None = None,
    fetcher: Fetcher | None = None,
    dry_run: bool = False,
) -> RunReport:
    """Execute one ingestion run and return its report.

    `scheme_ids=None` runs the whole corpus; a subset serves the P6.8
    `workflow_dispatch` single-scheme path and the 2.9 `--scheme` flag.

    `dry_run=True` still fetches and parses — it is a preview of *this* run, not
    a simulation — but touches nothing on disk: no schema is created, no
    `documents` are written, and no `runs` row is logged, because a preview did
    not happen and the run log must not claim it did. It works by stopping at
    `detect_changes`, the pure half P2.7 separated out for exactly this.

    `fetcher` is injectable so tests can drive the whole pipeline over
    `httpx.MockTransport` without touching the network.
    """
    sources = sources or get_sources()
    db_path = db_path or get_settings().registry_db
    targets = scheme_ids or [s.scheme_id for s in sources.schemes]
    for scheme_id in targets:
        sources.scheme(scheme_id)  # raises KeyError before any network traffic

    if not dry_run:
        db.init_db(db_path)
    previously_held = _held_doc_ids(db_path)

    started = datetime.now(UTC)
    started_at = started.isoformat(timespec="seconds")
    report = RunReport(run_id=_new_run_id(started), started_at=started_at, dry_run=dry_run)

    if not dry_run:
        with db.session(db_path) as conn:
            _open_run(conn, report.run_id, started_at)

    log.info(
        "run started",
        extra={"run_id": report.run_id, "schemes": targets, "dry_run": dry_run},
    )

    owns_fetcher = fetcher is None
    fetcher = fetcher or build_fetcher(sources, get_refusal_links())
    try:
        for scheme_id in targets:
            report.schemes.append(_ingest_scheme(fetcher, sources, scheme_id, previously_held))
        report.requests_made = fetcher.stats.requests_made
        report.cache_hits = fetcher.stats.cache_hits
    finally:
        if owns_fetcher:
            fetcher.close()

    # The registry write happens once, for every scheme at once, and only if
    # every scheme parsed. See the module docstring: a partial index locally
    # would not match what CI publishes on the same failure.
    if not report.failed_schemes:
        cards = [card for scheme in report.schemes for card in scheme.cards]
        if dry_run:
            report.decisions = detect_changes(cards, _previous_for(db_path, cards))
            report.summary = summarise(report.decisions)
        else:
            with db.session(db_path) as conn:
                report.decisions, report.summary = reconcile(conn, cards, now=started_at)
            report.committed = True
    else:
        log.error(
            "registry not written — a scheme failed",
            extra={
                "run_id": report.run_id,
                "failed": [s.scheme_id for s in report.failed_schemes],
            },
        )

    report.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    if not dry_run:
        with db.session(db_path) as conn:
            _close_run(conn, report)

    log.info(
        "run finished",
        extra={
            "run_id": report.run_id,
            "status": report.status,
            **report.run_row(),
            "new": report.summary.new,
            "changed": report.summary.changed,
            "refreshed": report.summary.refreshed,
            "unchanged": report.summary.unchanged,
            "conflicts": len(report.conflicts),
            "regressions": len(report.regressions),
            "requests": report.requests_made,
            "to_embed": len(report.cards_to_embed()),
        },
    )
    return report


def format_report(report: RunReport) -> str:
    """Human-readable run summary. Used by the 2.9 CLI and by `--help`-less runs."""
    label = "DRY RUN" if report.dry_run else "run"
    lines = [
        f"{label} {report.run_id}  [{report.status.upper()}]",
        f"  {report.started_at} -> {report.finished_at}",
        f"  {report.requests_made} request(s), {report.cache_hits} cache hit(s)",
        "",
    ]
    for scheme in report.schemes:
        if scheme.ok:
            note = f"{len(scheme.cards)}/{scheme.requested} facts"
            if scheme.missing:
                note += f", {len(scheme.missing)} unavailable"
        else:
            note = f"FAILED — {scheme.error}"
        lines.append(f"  {scheme.scheme_id:18} {note}")

    s = report.summary
    lines += [
        "",
        f"  new {s.new}   changed {s.changed}   refreshed {s.refreshed}   "
        f"unchanged {s.unchanged}   conflicts {len(s.conflicts)}",
        f"  to embed: {len(report.cards_to_embed())}   "
        f"to restamp: {len(report.cards_to_restamp())}",
    ]
    if report.dry_run:
        lines.append("  DRY RUN — nothing written; this is what a real run would do")
    elif not report.committed:
        lines.append("  registry NOT written — a scheme failed; previous index stands")
    for gap in report.regressions:
        lines.append(f"  REGRESSION {gap.doc_id}: {gap.reason}")
    for conflict in report.conflicts:
        lines.append(f"  CONFLICT   {conflict.doc_id}: {conflict.reason}")
    return "\n".join(lines)
