"""SQLite registry: documents + runs (P1.4, ARCH §9).

Two tables, and the distinction between two timestamp columns is the point of
the whole schema:

  fetched_at    — when the scheduler last retrieved the source
  source_as_of  — the as-of date printed IN the source

The answer footer and the freshness gate use `source_as_of`, NEVER `fetched_at`
(PS §8.3). A daily run does not make a document fresh; it only proves the
upstream copy has not changed. Conflating them is the easy way to launder stale
data as current — which is precisely what PS §4.5 exists to prevent.

There is no `active_collection` pointer: the git commit is the atomic swap
(ARCH §8.4).
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per FACT, not per page. A Groww page yields many facts, each with its
-- own source_as_of and max_age, and they do not update together (ARCH §14).
CREATE TABLE IF NOT EXISTS documents (
    doc_id          TEXT PRIMARY KEY,           -- "{scheme_id}:{doc_type}"
    scheme_id       TEXT NOT NULL,
    doc_type        TEXT NOT NULL,
    source_url      TEXT NOT NULL,              -- the citation shown to users
    content_hash    TEXT NOT NULL,              -- hash of the extracted fact VALUE, not the page
                                                -- and not the card text. Page-level hashing would
                                                -- flip daily on the volatile __NEXT_DATA__ nonce
                                                -- (P0.1); this is what moves iff the fact moves,
                                                -- so it is what the I-09 date check compares.
    card_hash       TEXT NOT NULL DEFAULT '',   -- hash of the EMBEDDED CARD TEXT. Separate from
                                                -- content_hash on purpose: "did the fact move?"
                                                -- is a compliance question, "must we re-embed?"
                                                -- is a cost question, and a card-renderer revision
                                                -- answers them differently (P2.7).
    source_as_of    TEXT NOT NULL,              -- ISO date; drives footer + freshness gate
    fetched_at      TEXT NOT NULL,              -- ISO datetime; observability only
    last_changed_at TEXT,                       -- ISO datetime; powers missing-update alerts
    status          TEXT NOT NULL DEFAULT 'ok'
                    CHECK (status IN ('ok', 'failed', 'stale')),
    UNIQUE (scheme_id, doc_type)
);

CREATE INDEX IF NOT EXISTS idx_documents_scheme  ON documents (scheme_id);
CREATE INDEX IF NOT EXISTS idx_documents_type    ON documents (doc_type);
CREATE INDEX IF NOT EXISTS idx_documents_as_of   ON documents (source_as_of);

-- The evidence behind the freshness claim (PS §4.5 behaviour 9).
CREATE TABLE IF NOT EXISTS runs (
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

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with sane defaults. Creates parent dirs if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Migrations for databases created by an EARLIER schema version. `SCHEMA` above
# always describes the current shape, so a fresh database needs none of these —
# `CREATE TABLE IF NOT EXISTS` is a no-op on a table that already exists, which
# is exactly why an in-place `ALTER` is required for an older one.
#
# Keyed by the version each migration produces. Statements must be idempotent in
# effect; `_migrate` additionally skips any ADD COLUMN whose column is present.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: ("ALTER TABLE documents ADD COLUMN card_hash TEXT NOT NULL DEFAULT ''",),
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection, from_version: int | None) -> None:
    """Bring an existing database up to SCHEMA_VERSION.

    `from_version is None` means the database was just created from `SCHEMA`,
    which is already current — there is nothing to migrate.
    """
    if from_version is None or from_version >= SCHEMA_VERSION:
        return
    if from_version < 1:
        raise RuntimeError(f"unknown schema_version {from_version}; refusing to migrate")

    for version in range(from_version + 1, SCHEMA_VERSION + 1):
        for statement in _MIGRATIONS.get(version, ()):
            # An interrupted earlier attempt can leave a column already added.
            # Re-running ALTER would raise and wedge every subsequent run.
            if statement.startswith("ALTER TABLE documents ADD COLUMN"):
                column = statement.split("ADD COLUMN", 1)[1].split()[0]
                if column in _columns(conn, "documents"):
                    continue
            conn.execute(statement)


def init_db(db_path: Path) -> None:
    """Create the schema if absent, migrate it if old. Idempotent — safe on every run."""
    existing = get_schema_version(db_path)
    with contextlib.closing(connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn, existing)
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )


def get_schema_version(db_path: Path) -> int | None:
    """The version a database was last initialised at, or None if it has none.

    None covers three cases that all mean "nothing to migrate": no file, a file
    with no tables (a crash between create and `executescript`), and a file with
    tables but no `schema_meta` row. `init_db` calls this on every run, so it
    must not raise on a half-built database — that would wedge the pipeline.
    """
    if not db_path.exists():
        return None
    with contextlib.closing(connect(db_path)) as conn:
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return int(row["value"]) if row else None


@contextlib.contextmanager
def session(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Transactional scope. Commits on success, rolls back on any exception.

    Ingestion writes many rows per run; a partial write would leave the registry
    disagreeing with the index it describes.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def document_count(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    with contextlib.closing(connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]


def last_successful_run(db_path: Path) -> dict | None:
    if not db_path.exists():
        return None
    with contextlib.closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE status = 'success' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None
