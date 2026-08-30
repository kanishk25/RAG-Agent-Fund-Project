"""Per-fact change detection against the registry (P2.7, ARCH §8.2b/§8.3).

Every scheme page is fetched daily, and almost nothing on it moves. This module
decides, for each fact, whether the run must re-embed it, merely re-date it, or
leave it alone — and it refuses to do any of those quietly when the evidence
does not hang together.

**Hash per fact, not per page.** A Groww page carries seven facts under one
`nav_date`, and NAV moves every business day. Comparing a page-level hash would
mark all seven changed every day and re-embed the whole corpus nightly, which
destroys the cost argument for the daily cadence (ARCH §8.3). One row, one hash,
one decision.

**Two hashes per fact, because two different questions are being asked.**
`content_hash` (the parsed value) answers *did the fact move?*; `card_hash` (the
embedded sentence) answers *must we re-embed?* See `FactCard` for why collapsing
them into one would make a card-renderer edit look like upstream tampering
across all five schemes at once.

The five outcomes
-----------------

`NEW` — no prior row. INSERT, and embed.

`UNCHANGED` — value, card text and date all identical. Touch `fetched_at`, the
one column that says "we looked"; nothing else moves and nothing is embedded.

`REFRESHED` — the date advanced but value and card text did not (I-10). Update
the date; **do not re-embed**, because the text about to be embedded is
byte-identical to the text already in the index.

`CHANGED` — value or card text moved, and the date moved with it. Full update
and re-embed.

`CONFLICT` — the value and the date disagree (I-09), or the date went backwards.
The row is left exactly as it was apart from `fetched_at`, and quarantined as
`failed`. Nothing is embedded.

`REFRESHED` is the one that earns its keep. `min_sip`, `lock_in` and `benchmark`
are dated by `nav_date` (PS §9), so every business day their `source_as_of`
advances while their value does not. Without this outcome those three facts
would re-embed daily on every scheme — and the exit criterion "a NAV change does
not mark the page's other facts as changed" would fail.

Why `CONFLICT` rejects rather than accepts
------------------------------------------

Edge case I-09, previously marked ⚠ DECIDE, is **decided here as reject + alert**.

The system's central promise (PS §4.5) is that the footer date describes the
value shown. If Groww moves an expense ratio without moving its `as_on_date`,
accepting the new number would serve it under the old date and break precisely
that promise — silently, and in the most authoritative-looking way possible.
Refusing the update instead leaves the previous value standing under its own
correct date: stale, but true, and ageing visibly toward the §7.3 freshness gate
which will eventually refuse it outright. Stale-and-honest degrades safely;
new-value-old-date does not.

A `source_as_of` that moves *backwards* is treated the same way, for the same
reason: it means the fetch saw an older snapshot than the registry already
holds, so whichever of the two is real, we do not know which — and the stored
one at least has a date it can defend.

Both cases are sticky by construction. The conflict recurs on every subsequent
run until the date moves or upstream reverts, so it cannot be missed by a single
overlooked log line. `ChangeSummary.conflicts` is what P2.8 exits non-zero on
and P6.7 raises an issue from.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from mf_faq.ingest.fact_card import FactCard
from mf_faq.logging_setup import get_logger

log = get_logger(__name__)


class Outcome(StrEnum):
    NEW = "new"
    UNCHANGED = "unchanged"
    REFRESHED = "refreshed"
    CHANGED = "changed"
    CONFLICT = "conflict"


#: Outcomes whose card text must be (re-)written to the vector index in P3.
#: `REFRESHED` is deliberately absent — its text is byte-identical to what is
#: already embedded, and only the stamped `source_as_of` metadata needs updating.
NEEDS_EMBEDDING = frozenset({Outcome.NEW, Outcome.CHANGED})

#: Outcomes whose Chroma metadata must be restamped even without re-embedding.
NEEDS_METADATA_UPDATE = frozenset({Outcome.NEW, Outcome.CHANGED, Outcome.REFRESHED})


@dataclass(frozen=True)
class PreviousDoc:
    """The registry's view of one fact, as of the last run that wrote it."""

    doc_id: str
    content_hash: str
    card_hash: str
    source_as_of: str
    status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PreviousDoc:
        return cls(
            doc_id=row["doc_id"],
            content_hash=row["content_hash"],
            card_hash=row["card_hash"] or "",
            source_as_of=row["source_as_of"],
            status=row["status"],
        )


@dataclass(frozen=True)
class Decision:
    """What this run concluded about one fact, and why."""

    card: FactCard
    outcome: Outcome
    reason: str
    previous: PreviousDoc | None = None

    @property
    def doc_id(self) -> str:
        return self.card.doc_id

    @property
    def needs_embedding(self) -> bool:
        return self.outcome in NEEDS_EMBEDDING

    @property
    def needs_metadata_update(self) -> bool:
        return self.outcome in NEEDS_METADATA_UPDATE

    @property
    def is_conflict(self) -> bool:
        return self.outcome is Outcome.CONFLICT


@dataclass
class ChangeSummary:
    """Per-run counts, shaped for the `runs` table and the P6 alerting step."""

    new: int = 0
    unchanged: int = 0
    refreshed: int = 0
    changed: int = 0
    conflicts: list[Decision] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return self.new + self.unchanged + self.refreshed + self.changed + len(self.conflicts)

    @property
    def embedded(self) -> int:
        """Facts whose text must be re-embedded — `NEW` plus `CHANGED`."""
        return self.new + self.changed

    def record(self, decision: Decision) -> None:
        match decision.outcome:
            case Outcome.NEW:
                self.new += 1
            case Outcome.UNCHANGED:
                self.unchanged += 1
            case Outcome.REFRESHED:
                self.refreshed += 1
            case Outcome.CHANGED:
                self.changed += 1
            case Outcome.CONFLICT:
                self.conflicts.append(decision)

    def as_row(self) -> dict[str, int]:
        """Change detection's contribution to the `runs` counters (ARCH §9).

        `sources_changed` counts rows this run actually modified, so a
        `REFRESHED` date counts even though nothing was re-embedded — the
        registry diff is real and it is what gets committed.

        P2.8 merges these with the fetch and parse failures it sees, which this
        module never observes: a fact that failed to parse produces no card and
        so reaches no decision here.
        """
        return {
            "sources_attempted": self.attempted,
            "sources_changed": self.embedded + self.refreshed,
            "sources_failed": len(self.conflicts),
        }


# --------------------------------------------------------------------------
# Detection — pure, no I/O
# --------------------------------------------------------------------------


def decide(card: FactCard, previous: PreviousDoc | None) -> Decision:
    """Classify one fact against its registry row.

    Ordering matters: the two integrity checks (date regression, then I-09) run
    before the cheap equality checks, so a suspect fact can never fall through
    into an accepted update.
    """
    if previous is None:
        return Decision(card, Outcome.NEW, "no previous registry row")

    value_moved = card.value_hash != previous.content_hash
    text_moved = card.text_hash != previous.card_hash
    date_moved = card.source_as_of != previous.source_as_of

    if card.source_as_of < previous.source_as_of:
        return Decision(
            card,
            Outcome.CONFLICT,
            f"source_as_of went backwards: registry holds {previous.source_as_of}, "
            f"this fetch says {card.source_as_of} — the fetch saw an older snapshot "
            "than we already trust",
            previous,
        )

    if value_moved and not date_moved:
        # I-09. The dangerous one: a new number under an old date.
        return Decision(
            card,
            Outcome.CONFLICT,
            f"value changed but source_as_of did not (still {previous.source_as_of}) — "
            "accepting this would serve a new value under a stale footer date (I-09)",
            previous,
        )

    if value_moved:
        return Decision(
            card,
            Outcome.CHANGED,
            f"value changed; source_as_of {previous.source_as_of} -> {card.source_as_of}",
            previous,
        )

    if text_moved:
        # The value stands still but its rendering does not — a card-renderer
        # revision, not upstream movement. Re-embedding is correct and this is
        # emphatically NOT an I-09 conflict: no new fact is being backdated.
        return Decision(
            card,
            Outcome.CHANGED,
            "card text changed while the value did not — card rendering revised",
            previous,
        )

    if date_moved:
        return Decision(
            card,
            Outcome.REFRESHED,
            f"source_as_of {previous.source_as_of} -> {card.source_as_of}, "
            "value and card text identical — re-dated, not re-embedded (I-10)",
            previous,
        )

    return Decision(card, Outcome.UNCHANGED, "value, card text and source_as_of all identical")


def summarise(decisions: Sequence[Decision]) -> ChangeSummary:
    """Count decisions without applying them.

    Exists so a caller can see what a run *would* do — the pipeline's
    `--dry-run` — using the same counters a real run reports.
    """
    summary = ChangeSummary()
    for decision in decisions:
        summary.record(decision)
    return summary


def detect_changes(cards: Sequence[FactCard], previous: dict[str, PreviousDoc]) -> list[Decision]:
    """Classify a run's cards against the registry snapshot. No I/O, no writes."""
    decisions = [decide(card, previous.get(card.doc_id)) for card in cards]

    for decision in decisions:
        if decision.is_conflict:
            log.error(
                "change conflict — update rejected",
                extra={"doc_id": decision.doc_id, "reason": decision.reason},
            )
        else:
            log.debug(
                "change decision",
                extra={
                    "doc_id": decision.doc_id,
                    "outcome": decision.outcome.value,
                    "reason": decision.reason,
                },
            )
    return decisions


# --------------------------------------------------------------------------
# Registry I/O
# --------------------------------------------------------------------------


def load_previous(conn: sqlite3.Connection, doc_ids: Iterable[str]) -> dict[str, PreviousDoc]:
    """Fetch the registry rows for the given doc_ids. Missing ids are simply absent."""
    ids = list(dict.fromkeys(doc_ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT doc_id, content_hash, card_hash, source_as_of, status "
        f"FROM documents WHERE doc_id IN ({placeholders})",
        ids,
    )
    return {row["doc_id"]: PreviousDoc.from_row(row) for row in rows}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def apply_changes(
    conn: sqlite3.Connection, decisions: Sequence[Decision], *, now: str | None = None
) -> ChangeSummary:
    """Write each decision to `documents` and return the run's counts.

    Every outcome touches `fetched_at` — including `CONFLICT`, because the fetch
    itself succeeded and observability should say so. Only `NEW` and `CHANGED`
    move `last_changed_at`: it means "when our stored content last moved", which
    is what the §8.5 missing-update checks compare against.

    Call inside `db.session()`. A half-applied run would leave the registry
    disagreeing with the index it describes.
    """
    fetched_at = now or _now_iso()

    for decision in decisions:
        card = decision.card
        match decision.outcome:
            case Outcome.NEW:
                conn.execute(
                    "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
                    "content_hash, card_hash, source_as_of, fetched_at, last_changed_at, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'ok')",
                    (
                        card.doc_id,
                        card.scheme_id,
                        card.doc_type,
                        card.source_url,
                        card.value_hash,
                        card.text_hash,
                        card.source_as_of,
                        fetched_at,
                        fetched_at,
                    ),
                )
            case Outcome.CHANGED:
                conn.execute(
                    "UPDATE documents SET source_url = ?, content_hash = ?, card_hash = ?, "
                    "source_as_of = ?, fetched_at = ?, last_changed_at = ?, status = 'ok' "
                    "WHERE doc_id = ?",
                    (
                        card.source_url,
                        card.value_hash,
                        card.text_hash,
                        card.source_as_of,
                        fetched_at,
                        fetched_at,
                        card.doc_id,
                    ),
                )
            case Outcome.REFRESHED:
                # The date and nothing else. `last_changed_at` stays put because
                # the content did not change — moving it would make a fact that
                # has been frozen for months look freshly updated to §8.5.
                conn.execute(
                    "UPDATE documents SET source_url = ?, source_as_of = ?, fetched_at = ?, "
                    "status = 'ok' WHERE doc_id = ?",
                    (card.source_url, card.source_as_of, fetched_at, card.doc_id),
                )
            case Outcome.UNCHANGED:
                # Clears a quarantine, and only a quarantine. If a prior run
                # conflicted and upstream has since reverted to the value we
                # already hold, the row is trustworthy again — otherwise
                # `failed` would stick forever. `stale`, which a freshness sweep
                # owns, is deliberately left alone.
                conn.execute(
                    "UPDATE documents SET fetched_at = ?, "
                    "status = CASE WHEN status = 'failed' THEN 'ok' ELSE status END "
                    "WHERE doc_id = ?",
                    (fetched_at, card.doc_id),
                )
            case Outcome.CONFLICT:
                # Content, hashes and date are all left exactly as they were —
                # the stored value keeps the date it can defend. `failed`
                # quarantines the row for P4 and keeps the signal durable after
                # this run's logs have rotated.
                conn.execute(
                    "UPDATE documents SET fetched_at = ?, status = 'failed' WHERE doc_id = ?",
                    (fetched_at, card.doc_id),
                )

    return summarise(decisions)


def reconcile(
    conn: sqlite3.Connection, cards: Sequence[FactCard], *, now: str | None = None
) -> tuple[list[Decision], ChangeSummary]:
    """Detect and apply in one step. The entry point P2.8 calls."""
    previous = load_previous(conn, (c.doc_id for c in cards))
    decisions = detect_changes(cards, previous)
    return decisions, apply_changes(conn, decisions, now=now)
