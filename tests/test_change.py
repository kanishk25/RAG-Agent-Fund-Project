"""Change-detection tests (P2.7).

Two P2 exit criteria live here and are the reason the module exists:

  * "Re-running immediately reports 0 changed" — if this fails, normalisation is
    leaking volatile markup and the daily-cadence cost argument collapses.
  * "A NAV change does not mark the page's other facts as changed" — the whole
    point of hashing per fact rather than per page.

The third load-bearing group is I-09: a value that moves without its date moving
must be rejected, not accepted, because accepting it would serve a new number
under a stale footer date.
"""

from __future__ import annotations

import contextlib
import copy
import json
from datetime import date
from pathlib import Path

import pytest

from mf_faq import db
from mf_faq.ingest.change import (
    ChangeSummary,
    Outcome,
    PreviousDoc,
    apply_changes,
    decide,
    detect_changes,
    load_previous,
    reconcile,
)
from mf_faq.ingest.fact_card import render_cards
from mf_faq.ingest.parse import parse_facts
from mf_faq.settings import get_sources

FIXTURES = Path(__file__).parent / "fixtures" / "groww"
TODAY = date(2026, 8, 30)
SCHEME_IDS = ["mo_large_midcap", "mo_bse_value", "mo_elss", "mo_next50", "mo_bse_fin"]


def payload(scheme_id: str) -> dict:
    return json.loads((FIXTURES / f"{scheme_id}.json").read_text(encoding="utf-8"))


def cards_for(scheme_id: str, data: dict | None = None, doc_types=None):
    scheme = get_sources().scheme(scheme_id)
    facts, _ = parse_facts(
        data if data is not None else payload(scheme_id),
        scheme_id=scheme_id,
        source_url=str(scheme.url),
        doc_types=doc_types or scheme.extract,
        today=TODAY,
    )
    return render_cards(facts, scheme)


def by_type(decisions):
    return {d.card.doc_type: d for d in decisions}


@pytest.fixture
def fresh_db(tmp_path):
    path = tmp_path / "registry.db"
    db.init_db(path)
    return path


@contextlib.contextmanager
def conn_for(path):
    with contextlib.closing(db.connect(path)) as conn:
        yield conn


def ingest(path, cards, *, now="2026-08-30T18:00:00+00:00"):
    with db.session(path) as conn:
        return reconcile(conn, cards, now=now)


def rows(path) -> dict[str, dict]:
    with conn_for(path) as conn:
        return {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM documents")}


# -- the two exit criteria -------------------------------------------------


def test_first_run_is_all_new_and_populates_the_registry(fresh_db):
    cards = [c for sid in SCHEME_IDS for c in cards_for(sid)]
    decisions, summary = ingest(fresh_db, cards)

    assert summary.new == len(cards)
    assert summary.unchanged == summary.refreshed == summary.changed == 0
    assert summary.conflicts == []
    assert all(d.needs_embedding for d in decisions)

    stored = rows(fresh_db)
    assert len(stored) == len(cards)
    # Distinct source_as_of values across doc_types is what makes the per-fact
    # freshness policy meaningful in the first place.
    assert len({r["source_as_of"] for r in stored.values()}) > 1
    for card in cards:
        row = stored[card.doc_id]
        assert row["content_hash"] == card.value_hash
        assert row["card_hash"] == card.text_hash
        assert row["source_as_of"] == card.source_as_of
        assert row["last_changed_at"] == row["fetched_at"]
        assert row["status"] == "ok"


def test_rerunning_immediately_reports_zero_changed(fresh_db):
    """P2 exit criterion. An unchanged corpus must re-embed nothing."""
    cards = [c for sid in SCHEME_IDS for c in cards_for(sid)]
    ingest(fresh_db, cards)

    decisions, summary = ingest(fresh_db, cards, now="2026-08-31T18:00:00+00:00")

    assert summary.changed == 0
    assert summary.refreshed == 0
    assert summary.new == 0
    assert summary.embedded == 0
    assert summary.unchanged == len(cards)
    assert not any(d.needs_embedding for d in decisions)


def test_rerun_touches_fetched_at_but_not_last_changed_at(fresh_db):
    cards = cards_for("mo_elss")
    ingest(fresh_db, cards, now="2026-08-30T18:00:00+00:00")
    before = rows(fresh_db)

    ingest(fresh_db, cards, now="2026-08-31T18:00:00+00:00")
    after = rows(fresh_db)

    for doc_id, row in after.items():
        assert row["fetched_at"] == "2026-08-31T18:00:00+00:00"
        assert row["last_changed_at"] == before[doc_id]["last_changed_at"]


def test_nav_change_does_not_mark_the_pages_other_facts_as_changed(fresh_db):
    """P2 exit criterion — the reason hashing is per fact, not per page.

    A new NAV arrives with a new `nav_date`, which is also the `source_as_of`
    of min_sip / lock_in / benchmark (PS §9). Those three are therefore re-dated
    but NOT re-embedded; the rest of the page is untouched entirely.
    """
    ingest(fresh_db, cards_for("mo_elss"))

    tomorrow = copy.deepcopy(payload("mo_elss"))
    tomorrow["nav"] = 99.1234
    tomorrow["nav_date"] = "29-Aug-2026"

    decisions, summary = ingest(fresh_db, cards_for("mo_elss", tomorrow))
    outcomes = by_type(decisions)

    assert outcomes["nav"].outcome is Outcome.CHANGED
    assert summary.embedded == 1  # NAV and nothing else

    for doc_type in ("min_sip", "lock_in", "benchmark"):
        assert outcomes[doc_type].outcome is Outcome.REFRESHED
        assert not outcomes[doc_type].needs_embedding
        assert outcomes[doc_type].needs_metadata_update

    for doc_type in ("expense_ratio", "exit_load", "holdings"):
        assert outcomes[doc_type].outcome is Outcome.UNCHANGED


def test_nav_change_does_not_leak_across_schemes(fresh_db):
    cards = [c for sid in SCHEME_IDS for c in cards_for(sid)]
    ingest(fresh_db, cards)

    moved = copy.deepcopy(payload("mo_elss"))
    moved["nav"] = 99.1234
    moved["nav_date"] = "29-Aug-2026"
    next_run = [
        c for sid in SCHEME_IDS for c in cards_for(sid, moved if sid == "mo_elss" else None)
    ]

    decisions, _ = ingest(fresh_db, next_run)
    touched = {d.doc_id for d in decisions if d.outcome is not Outcome.UNCHANGED}

    assert all(doc_id.startswith("mo_elss:") for doc_id in touched)


# -- REFRESHED (I-10) ------------------------------------------------------


def test_expense_ratio_date_advances_daily_without_re_embedding(fresh_db):
    """`historic_fund_expense` has frequency Daily — the ELSS ratio held 0.97
    for 48 days while its `as_on_date` moved every one of them (P2.6)."""
    ingest(fresh_db, cards_for("mo_elss", doc_types=["expense_ratio"]))

    moved = copy.deepcopy(payload("mo_elss"))
    for row in moved["historic_fund_expense"]:
        if str(row["as_on_date"]).startswith("2026-08-28"):
            row["as_on_date"] = "2026-08-29T00:00:00"

    decisions, summary = ingest(fresh_db, cards_for("mo_elss", moved, ["expense_ratio"]))

    assert decisions[0].outcome is Outcome.REFRESHED
    assert summary.embedded == 0
    stored = rows(fresh_db)["mo_elss:expense_ratio"]
    assert stored["source_as_of"] == "2026-08-29"


def test_refreshed_updates_the_date_but_leaves_last_changed_at(fresh_db):
    """`last_changed_at` means "our content moved", which powers §8.5 alerting.

    Advancing it on a date-only refresh would make a fact frozen for months look
    freshly updated and silence the alert that exists to notice exactly that.
    """
    ingest(fresh_db, cards_for("mo_elss", doc_types=["benchmark"]))
    before = rows(fresh_db)["mo_elss:benchmark"]

    moved = copy.deepcopy(payload("mo_elss"))
    moved["nav_date"] = "29-Aug-2026"
    ingest(fresh_db, cards_for("mo_elss", moved, ["benchmark"]), now="2026-08-31T18:00:00+00:00")

    after = rows(fresh_db)["mo_elss:benchmark"]
    assert after["source_as_of"] == "2026-08-29"
    assert after["fetched_at"] == "2026-08-31T18:00:00+00:00"
    assert after["last_changed_at"] == before["last_changed_at"]
    assert after["content_hash"] == before["content_hash"]
    assert after["card_hash"] == before["card_hash"]


# -- CHANGED ---------------------------------------------------------------


def test_changed_value_updates_every_hash_and_both_timestamps(fresh_db):
    ingest(fresh_db, cards_for("mo_elss", doc_types=["nav"]))
    before = rows(fresh_db)["mo_elss:nav"]

    moved = copy.deepcopy(payload("mo_elss"))
    moved["nav"] = 99.1234
    moved["nav_date"] = "29-Aug-2026"
    ingest(fresh_db, cards_for("mo_elss", moved, ["nav"]), now="2026-08-31T18:00:00+00:00")

    after = rows(fresh_db)["mo_elss:nav"]
    assert after["content_hash"] != before["content_hash"]
    assert after["card_hash"] != before["card_hash"]
    assert after["source_as_of"] == "2026-08-29"
    assert after["last_changed_at"] == after["fetched_at"] == "2026-08-31T18:00:00+00:00"


def test_a_renderer_revision_re_embeds_without_raising_a_conflict(fresh_db):
    """The reason `content_hash` and `card_hash` are stored separately.

    Rewording a card changes the embedded text while the fact stands still. With
    a single hash this would read as "value moved, date didn't" — I-09 — and
    would quarantine all 35 facts the first time anyone edited a sentence.
    """
    cards = cards_for("mo_elss", doc_types=["benchmark"])
    ingest(fresh_db, cards)

    from dataclasses import replace

    reworded = [replace(c, text=c.text + " (reworded)") for c in cards]
    decisions, summary = ingest(fresh_db, reworded)

    assert decisions[0].outcome is Outcome.CHANGED
    assert "card rendering revised" in decisions[0].reason
    assert summary.conflicts == []
    assert summary.embedded == 1
    assert rows(fresh_db)["mo_elss:benchmark"]["status"] == "ok"


# -- CONFLICT (I-09 + date regression) -------------------------------------


def test_value_change_without_a_date_change_is_rejected(fresh_db):
    """I-09, decided as reject + alert.

    Accepting would serve a new expense ratio under the old "as on" date, which
    breaks the one guarantee PS §4.5 exists to protect.
    """
    ingest(fresh_db, cards_for("mo_elss", doc_types=["expense_ratio"]))
    before = rows(fresh_db)["mo_elss:expense_ratio"]

    tampered = copy.deepcopy(payload("mo_elss"))
    tampered["expense_ratio"] = 1.75  # new value, same as_on_date

    decisions, summary = ingest(
        fresh_db, cards_for("mo_elss", tampered, ["expense_ratio"]), now="2026-08-31T18:00:00+00:00"
    )

    assert decisions[0].outcome is Outcome.CONFLICT
    assert not decisions[0].needs_embedding
    assert summary.changed == 0
    assert [d.doc_id for d in summary.conflicts] == ["mo_elss:expense_ratio"]

    after = rows(fresh_db)["mo_elss:expense_ratio"]
    assert after["content_hash"] == before["content_hash"]  # the old value stands
    assert after["source_as_of"] == before["source_as_of"]  # under its own date
    assert after["last_changed_at"] == before["last_changed_at"]
    assert after["status"] == "failed"  # quarantined
    assert after["fetched_at"] == "2026-08-31T18:00:00+00:00"  # but we did look


def test_a_conflict_recurs_until_it_is_resolved(fresh_db):
    """Sticky by construction — it cannot be missed by one overlooked log line."""
    ingest(fresh_db, cards_for("mo_elss", doc_types=["expense_ratio"]))

    tampered = copy.deepcopy(payload("mo_elss"))
    tampered["expense_ratio"] = 1.75
    tampered_cards = cards_for("mo_elss", tampered, ["expense_ratio"])

    for _ in range(3):
        _, summary = ingest(fresh_db, tampered_cards)
        assert len(summary.conflicts) == 1


def test_a_reverted_upstream_value_clears_the_quarantine(fresh_db):
    """`failed` must not stick forever once the row is trustworthy again."""
    good = cards_for("mo_elss", doc_types=["expense_ratio"])
    ingest(fresh_db, good)

    tampered = copy.deepcopy(payload("mo_elss"))
    tampered["expense_ratio"] = 1.75
    ingest(fresh_db, cards_for("mo_elss", tampered, ["expense_ratio"]))
    assert rows(fresh_db)["mo_elss:expense_ratio"]["status"] == "failed"

    decisions, _ = ingest(fresh_db, good)
    assert decisions[0].outcome is Outcome.UNCHANGED
    assert rows(fresh_db)["mo_elss:expense_ratio"]["status"] == "ok"


def test_a_conflict_resolves_once_the_date_catches_up(fresh_db):
    ingest(fresh_db, cards_for("mo_elss", doc_types=["expense_ratio"]))

    tampered = copy.deepcopy(payload("mo_elss"))
    tampered["expense_ratio"] = 1.75
    ingest(fresh_db, cards_for("mo_elss", tampered, ["expense_ratio"]))

    dated = copy.deepcopy(tampered)
    for row in dated["historic_fund_expense"]:
        if str(row["as_on_date"]).startswith("2026-08-28"):
            row["as_on_date"] = "2026-08-29T00:00:00"

    decisions, summary = ingest(fresh_db, cards_for("mo_elss", dated, ["expense_ratio"]))
    assert decisions[0].outcome is Outcome.CHANGED
    assert summary.conflicts == []
    assert rows(fresh_db)["mo_elss:expense_ratio"]["status"] == "ok"


def test_a_backwards_source_as_of_is_rejected(fresh_db):
    """An older snapshot than the registry already holds. We cannot tell which
    is real, and the stored one at least has a date it can defend."""
    moved = copy.deepcopy(payload("mo_elss"))
    moved["nav_date"] = "29-Aug-2026"
    ingest(fresh_db, cards_for("mo_elss", moved, ["benchmark"]))

    decisions, summary = ingest(fresh_db, cards_for("mo_elss", doc_types=["benchmark"]))

    assert decisions[0].outcome is Outcome.CONFLICT
    assert "backwards" in decisions[0].reason
    assert len(summary.conflicts) == 1
    assert rows(fresh_db)["mo_elss:benchmark"]["source_as_of"] == "2026-08-29"


def test_backwards_date_wins_over_the_i09_check(fresh_db):
    """Both integrity checks could fire; the regression is the truer diagnosis."""
    moved = copy.deepcopy(payload("mo_elss"))
    moved["nav"] = 99.1234
    moved["nav_date"] = "29-Aug-2026"
    ingest(fresh_db, cards_for("mo_elss", moved, ["nav"]))

    decisions, _ = ingest(fresh_db, cards_for("mo_elss", doc_types=["nav"]))
    assert decisions[0].outcome is Outcome.CONFLICT
    assert "backwards" in decisions[0].reason


# -- unit-level decision matrix --------------------------------------------


def _prev(card, **overrides):
    base = {
        "doc_id": card.doc_id,
        "content_hash": card.value_hash,
        "card_hash": card.text_hash,
        "source_as_of": card.source_as_of,
        "status": "ok",
    }
    return PreviousDoc(**{**base, **overrides})


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (None, Outcome.NEW),
        ({}, Outcome.UNCHANGED),
        ({"source_as_of": "2026-08-01"}, Outcome.REFRESHED),
        ({"content_hash": "x", "source_as_of": "2026-08-01"}, Outcome.CHANGED),
        ({"card_hash": "x"}, Outcome.CHANGED),
        ({"content_hash": "x"}, Outcome.CONFLICT),
        ({"source_as_of": "2099-01-01"}, Outcome.CONFLICT),
    ],
)
def test_decision_matrix(overrides, expected):
    card = cards_for("mo_elss", doc_types=["benchmark"])[0]
    previous = None if overrides is None else _prev(card, **overrides)
    assert decide(card, previous).outcome is expected


def test_detect_changes_performs_no_writes(fresh_db):
    """Detection is pure — P2.8 may inspect decisions before committing to them."""
    cards = cards_for("mo_elss")
    with conn_for(fresh_db) as conn:
        detect_changes(cards, load_previous(conn, [c.doc_id for c in cards]))
    assert db.document_count(fresh_db) == 0


def test_load_previous_ignores_unknown_ids(fresh_db):
    ingest(fresh_db, cards_for("mo_elss", doc_types=["nav"]))
    with conn_for(fresh_db) as conn:
        found = load_previous(conn, ["mo_elss:nav", "mo_elss:nav", "nope:nav"])
    assert set(found) == {"mo_elss:nav"}


def test_load_previous_on_empty_input(fresh_db):
    with conn_for(fresh_db) as conn:
        assert load_previous(conn, []) == {}


def test_apply_changes_rolls_back_as_one_unit(fresh_db):
    """A half-applied run would leave the registry disagreeing with the index."""
    cards = cards_for("mo_elss")
    with pytest.raises(RuntimeError), db.session(fresh_db) as conn:
        previous = load_previous(conn, [c.doc_id for c in cards])
        apply_changes(conn, detect_changes(cards, previous))
        raise RuntimeError("simulated failure later in the pipeline")
    assert db.document_count(fresh_db) == 0


def test_summary_counters_feed_the_runs_table():
    summary = ChangeSummary(new=2, unchanged=30, refreshed=3, changed=1)
    assert summary.attempted == 36
    assert summary.embedded == 3
    assert summary.as_row() == {
        "sources_attempted": 36,
        "sources_changed": 6,
        "sources_failed": 0,
    }


# -- migration -------------------------------------------------------------

# The P1.4 shape, verbatim. Kept as a literal rather than derived from
# `db.SCHEMA` so that editing the current schema cannot silently redefine what
# "version 1" meant and make this test pass against itself.
V1_SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE documents (
    doc_id          TEXT PRIMARY KEY,
    scheme_id       TEXT NOT NULL,
    doc_type        TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    source_as_of    TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    last_changed_at TEXT,
    status          TEXT NOT NULL DEFAULT 'ok'
                    CHECK (status IN ('ok', 'failed', 'stale')),
    UNIQUE (scheme_id, doc_type)
);
CREATE TABLE runs (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'success', 'failed')),
    sources_attempted INTEGER NOT NULL DEFAULT 0,
    sources_changed   INTEGER NOT NULL DEFAULT 0,
    sources_failed    INTEGER NOT NULL DEFAULT 0,
    error_detail      TEXT
);
"""


def test_a_v1_registry_migrates_to_v2_keeping_its_rows(tmp_path):
    """`card_hash` arrived in P2.7; existing registries must not need a rebuild."""
    path = tmp_path / "registry.db"
    with contextlib.closing(db.connect(path)) as conn:
        conn.executescript(V1_SCHEMA)
        conn.execute(
            "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
            "content_hash, source_as_of, fetched_at) VALUES (?,?,?,?,?,?,?)",
            ("mo_elss:nav", "mo_elss", "nav", "https://groww.in/x", "h", "2026-08-28", "t"),
        )
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')")
        assert "card_hash" not in {r[1] for r in conn.execute("PRAGMA table_info(documents)")}

    db.init_db(path)

    assert db.get_schema_version(path) == db.SCHEMA_VERSION
    assert db.document_count(path) == 1
    assert rows(path)["mo_elss:nav"]["card_hash"] == ""


def test_migration_is_idempotent(fresh_db):
    db.init_db(fresh_db)
    db.init_db(fresh_db)
    assert db.get_schema_version(fresh_db) == db.SCHEMA_VERSION


def test_a_v1_row_with_no_card_hash_re_embeds_once(fresh_db):
    """Migrated rows carry `card_hash = ''`, which cannot match any real hash.

    That is the correct outcome: the pre-2.7 registry never recorded what text
    was embedded, so the first post-migration run must re-embed to find out —
    and it must do so as CHANGED, not as an I-09 conflict.
    """
    card = cards_for("mo_elss", doc_types=["benchmark"])[0]
    with db.session(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
            "content_hash, source_as_of, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (
                card.doc_id,
                card.scheme_id,
                card.doc_type,
                card.source_url,
                card.value_hash,
                card.source_as_of,
                "2026-08-29T18:00:00+00:00",
            ),
        )

    decisions, summary = ingest(fresh_db, [card])
    assert decisions[0].outcome is Outcome.CHANGED
    assert summary.conflicts == []
    assert rows(fresh_db)[card.doc_id]["card_hash"] == card.text_hash
