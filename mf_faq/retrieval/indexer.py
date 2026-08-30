"""The P2 → P3 seam: turn a finished ingestion run into an updated index.

P2.8 stops at a correct registry and hands over `RunReport`. This module is the
other side of that handover, and it exists as its own layer for one reason:
**the pipeline must stay importable without torch.** `pipeline.py` is what the
nightly job, the dry-run preview and twenty-two tests import; making it depend
on `sentence_transformers` would put a multi-hundred-megabyte import in front of
`--dry-run`, which deliberately touches nothing.

Three rules about when the index may be written
-----------------------------------------------
**A dry run never indexes.** `--dry-run` promises to touch nothing; an index
write is a write. Attempting one is a programming error, so it raises rather
than silently no-ops.

**A failed run never indexes.** When a scheme fails, P2.8 skips the registry
write entirely — so indexing would push the index *ahead* of the registry
describing it, on precisely the runs where the two most need to agree.

**A conflicted fact is never indexed.** I-09 quarantine means the registry kept
the previous value; the index keeps the matching previous text. `sync()` is told
which doc_ids those are and leaves them alone.

Ordering, and why the index is second
-------------------------------------
The registry is written first, the index second, and they are not one
transaction — SQLite and Chroma cannot be. So a crash in between leaves the
registry ahead of the index. That is survivable *only* because `store.sync()`
reconciles against the index's real contents rather than against this run's
decisions: the next run notices the missing chunks and re-embeds them
(`SyncReport.repaired`). Ordering it the other way round would not be
survivable, because nothing re-derives a registry from an index.

A sync failure still fails the run. The registry has moved and the index has
not, and while the next run will repair it, publishing that state would ship a
commit whose index does not match its own registry.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from mf_faq import db
from mf_faq.ingest.fact_card import FactCard
from mf_faq.ingest.pipeline import RunReport
from mf_faq.logging_setup import get_logger
from mf_faq.retrieval.store import SyncReport, VectorStore
from mf_faq.settings import get_settings

log = get_logger(__name__)


def registry_doc_ids(db_path: Path, scheme_ids: list[str]) -> set[str]:
    """doc_ids `documents` holds for these schemes — the deletion baseline.

    Deletion is driven by the registry, not by the run's cards, so a fact the
    page stopped yielding keeps its chunk while P2.8 keeps its row: the two
    artifacts age toward the freshness gate together instead of one vanishing.
    """
    if not db_path.exists() or not scheme_ids:
        return set()
    placeholders = ",".join("?" * len(scheme_ids))
    with contextlib.closing(db.connect(db_path)) as conn:
        rows = conn.execute(
            f"SELECT doc_id FROM documents WHERE scheme_id IN ({placeholders})",  # noqa: S608
            scheme_ids,
        )
        return {row["doc_id"] for row in rows}


def sync_from_run(
    report: RunReport,
    *,
    db_path: Path | None = None,
    store: VectorStore | None = None,
) -> SyncReport:
    """Bring the index in line with the registry this run just wrote."""
    if report.dry_run:
        raise ValueError("a dry run must not write the index")
    if report.failed_schemes:
        raise ValueError("a failed run must not write the index — the registry was not written")

    db_path = db_path or get_settings().registry_db
    store = store or VectorStore.open()

    scope = [s.scheme_id for s in report.schemes]
    cards: list[FactCard] = [card for scheme in report.schemes for card in scheme.cards]

    return store.sync(
        cards,
        scope=scope,
        registry_doc_ids=registry_doc_ids(db_path, scope),
        conflicted_doc_ids={d.doc_id for d in report.conflicts},
        decided_to_embed={c.doc_id for c in report.cards_to_embed()},
    )


def rebuild(
    report: RunReport,
    *,
    db_path: Path | None = None,
    store: VectorStore | None = None,
) -> SyncReport:
    """Drop the collection and embed everything this run produced.

    The escape hatch for a corrupt or schema-changed index. Ordinary runs use
    `sync_from_run`, which already repairs drift — reach for this only when the
    collection itself is the problem, since it re-embeds the whole corpus.
    """
    store = store or VectorStore.open()
    log.warning(
        "dropping collection for a full rebuild", extra={"collection": store.collection_name}
    )
    store.drop()
    return sync_from_run(report, db_path=db_path, store=store)
