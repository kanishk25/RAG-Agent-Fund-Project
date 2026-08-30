"""Registry schema tests (P1.4 / P1 exit criterion: empty DB initialises cleanly)."""

from __future__ import annotations

import contextlib
import sqlite3

import pytest

from mf_faq import db


@pytest.fixture
def fresh_db(tmp_path):
    path = tmp_path / "nested" / "registry.db"
    db.init_db(path)
    return path


def test_empty_db_initialises_from_scratch(fresh_db):
    """Exit criterion: creates parent dirs and both tables, no manual setup."""
    assert fresh_db.exists()
    assert db.get_schema_version(fresh_db) == db.SCHEMA_VERSION
    assert db.document_count(fresh_db) == 0
    assert db.last_successful_run(fresh_db) is None


def test_init_is_idempotent(fresh_db):
    """Runs on every ingest; a second call must not error or wipe data."""
    with db.session(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
            "content_hash, source_as_of, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (
                "mo_elss:nav",
                "mo_elss",
                "nav",
                "https://groww.in/x",
                "h1",
                "2026-08-28",
                "2026-08-30T18:00:00Z",
            ),
        )
    db.init_db(fresh_db)
    assert db.document_count(fresh_db) == 1


def test_missing_db_reports_zero_not_crash(tmp_path):
    """Health checks must work before the first ingest has ever run."""
    missing = tmp_path / "absent.db"
    assert db.get_schema_version(missing) is None
    assert db.document_count(missing) == 0


def test_one_row_per_scheme_and_doc_type(fresh_db):
    """A page yields many facts; each must be tracked separately, once."""
    with db.session(fresh_db) as conn:
        for doc_type in ("nav", "expense_ratio"):
            conn.execute(
                "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
                "content_hash, source_as_of, fetched_at) VALUES (?,?,?,?,?,?,?)",
                (
                    f"mo_elss:{doc_type}",
                    "mo_elss",
                    doc_type,
                    "https://groww.in/x",
                    "h",
                    "2026-08-28",
                    "2026-08-30T18:00:00Z",
                ),
            )
    assert db.document_count(fresh_db) == 2

    with pytest.raises(sqlite3.IntegrityError), db.session(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
            "content_hash, source_as_of, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (
                "other_id",
                "mo_elss",
                "nav",
                "https://groww.in/x",
                "h",
                "2026-08-28",
                "2026-08-30T18:00:00Z",
            ),
        )


def test_source_as_of_and_fetched_at_are_separate_columns(fresh_db):
    """PS §8.3 — the footer uses source_as_of; conflating them launders stale data.

    A doc re-fetched today with no upstream change keeps its original as-of date.
    """
    with db.session(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
            "content_hash, source_as_of, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (
                "mo_elss:benchmark",
                "mo_elss",
                "benchmark",
                "https://groww.in/x",
                "h",
                "2026-08-28",
                "2026-08-30T18:00:00Z",
            ),
        )
    with contextlib.closing(db.connect(fresh_db)) as conn:
        row = conn.execute("SELECT * FROM documents").fetchone()
    assert row["source_as_of"] == "2026-08-28"
    assert row["fetched_at"].startswith("2026-08-30")
    assert row["source_as_of"] != row["fetched_at"]


def test_status_column_rejects_unknown_values(fresh_db):
    with pytest.raises(sqlite3.IntegrityError), db.session(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
            "content_hash, source_as_of, fetched_at, status) VALUES (?,?,?,?,?,?,?,?)",
            ("x", "s", "nav", "u", "h", "2026-08-28", "2026-08-30T18:00:00Z", "weird"),
        )


def test_session_rolls_back_on_error(fresh_db):
    """A partial run must not leave the registry disagreeing with the index."""
    with contextlib.suppress(RuntimeError), db.session(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
            "content_hash, source_as_of, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (
                "mo_elss:nav",
                "mo_elss",
                "nav",
                "https://groww.in/x",
                "h",
                "2026-08-28",
                "2026-08-30T18:00:00Z",
            ),
        )
        raise RuntimeError("simulated mid-run failure")
    assert db.document_count(fresh_db) == 0


def test_runs_table_tracks_outcome(fresh_db):
    with db.session(fresh_db) as conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, finished_at, status, "
            "sources_attempted, sources_changed, sources_failed) VALUES (?,?,?,?,?,?,?)",
            ("r1", "2026-08-30T18:00:00Z", "2026-08-30T18:02:00Z", "success", 5, 2, 0),
        )
    run = db.last_successful_run(fresh_db)
    assert run is not None
    assert run["sources_changed"] == 2


def test_no_active_collection_table(fresh_db):
    """ARCH §8.4 — the git commit is the swap; a pointer table would be dead weight."""
    with contextlib.closing(db.connect(fresh_db)) as conn:
        tables = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "active_collection" not in tables
    assert {"documents", "runs"} <= tables


def test_registry_inspector_on_empty_db(fresh_db, capsys):
    """An empty registry must say so in words — a blank grid reads as a failure."""
    from mf_faq.registry import show

    assert show(fresh_db) == 0
    out = capsys.readouterr().out
    assert "No documents yet" in out
    assert "expected state until Phase 2" in out


def test_registry_inspector_on_missing_db(tmp_path, capsys):
    """Non-zero exit so a caller can distinguish 'absent' from 'empty'."""
    from mf_faq.registry import show

    assert show(tmp_path / "nope.db") == 1
    assert "does not exist yet" in capsys.readouterr().out


def test_registry_inspector_shows_rows(fresh_db, capsys):
    from mf_faq.registry import show

    with db.session(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
            "content_hash, source_as_of, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (
                "mo_elss:nav",
                "mo_elss",
                "nav",
                "https://groww.in/x",
                "h",
                "2026-08-28",
                "2026-08-30T18:00:00Z",
            ),
        )
    assert show(fresh_db) == 0
    out = capsys.readouterr().out
    assert "mo_elss:nav" in out
    assert "FRESHNESS by doc_type" in out
