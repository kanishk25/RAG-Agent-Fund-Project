"""Human-readable view of the registry: `python -m mf_faq.registry`.

Exists because an empty SQLite file and a broken one look identical in a GUI
viewer, and "0 rows" is a legitimate state before the first ingest. This prints
what is actually there, and says so in words when there is nothing.

Read-only by design — it never writes to the registry.
"""

from __future__ import annotations

import argparse
import contextlib
import sqlite3
from pathlib import Path

from mf_faq import db
from mf_faq.settings import get_settings


def _rows(conn: sqlite3.Connection, sql: str, *params) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params))


def _table(rows: list[sqlite3.Row], columns: list[str]) -> str:
    if not rows:
        return "  (no rows)"
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in columns}
    head = "  " + "  ".join(c.ljust(widths[c]) for c in columns)
    rule = "  " + "  ".join("-" * widths[c] for c in columns)
    body = ["  " + "  ".join(str(r[c]).ljust(widths[c]) for c in columns) for r in rows]
    return "\n".join([head, rule, *body])


def show(db_path: Path, scheme_id: str | None = None, runs_limit: int = 5) -> int:
    """Print registry contents. Returns a process exit code."""
    print(f"Registry: {db_path}")

    if not db_path.exists():
        print("\n  Database does not exist yet.")
        print("  Run the ingestion pipeline to create and populate it (Phase 2).")
        return 1

    version = db.get_schema_version(db_path)
    print(f"Schema version: {version}\n")

    with contextlib.closing(db.connect(db_path)) as conn:
        if scheme_id:
            docs = _rows(
                conn,
                "SELECT doc_id, doc_type, source_as_of, fetched_at, last_changed_at, status "
                "FROM documents WHERE scheme_id = ? ORDER BY doc_type",
                scheme_id,
            )
        else:
            docs = _rows(
                conn,
                "SELECT doc_id, doc_type, source_as_of, fetched_at, last_changed_at, status "
                "FROM documents ORDER BY scheme_id, doc_type",
            )

        print(f"DOCUMENTS ({len(docs)})")
        print(
            _table(
                docs,
                ["doc_id", "doc_type", "source_as_of", "fetched_at", "last_changed_at", "status"],
            )
        )

        if not docs:
            # The common case right after Phase 1 — say plainly that this is
            # expected rather than leaving an empty grid to be misread as failure.
            print("\n  No documents yet. The schema exists but nothing has been ingested.")
            print("  This is the expected state until Phase 2 runs.")

        runs = _rows(
            conn,
            "SELECT run_id, started_at, finished_at, status, sources_attempted, "
            "sources_changed, sources_failed FROM runs ORDER BY started_at DESC LIMIT ?",
            runs_limit,
        )
        print(f"\nRUNS (latest {len(runs)})")
        print(
            _table(
                runs,
                [
                    "run_id",
                    "started_at",
                    "finished_at",
                    "status",
                    "sources_attempted",
                    "sources_changed",
                    "sources_failed",
                ],
            )
        )

        if docs:
            # source_as_of vs fetched_at is the distinction the whole schema
            # turns on (PS §8.3), so surface it rather than burying it in a column.
            stale = _rows(
                conn,
                "SELECT doc_type, MIN(source_as_of) AS oldest, MAX(source_as_of) AS newest, "
                "COUNT(*) AS n FROM documents GROUP BY doc_type ORDER BY doc_type",
            )
            print("\nFRESHNESS by doc_type (source_as_of — NOT fetched_at)")
            print(_table(stale, ["doc_type", "oldest", "newest", "n"]))

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the mf-faq registry (read-only).")
    parser.add_argument("--db", type=Path, default=None, help="path to registry.db")
    parser.add_argument("--scheme", default=None, help="filter to one scheme_id")
    parser.add_argument("--runs", type=int, default=5, help="how many runs to show")
    args = parser.parse_args()
    return show(args.db or get_settings().registry_db, args.scheme, args.runs)


if __name__ == "__main__":
    raise SystemExit(main())
