"""The ingestion CLI (P2.9).

    python -m mf_faq.ingest --all                 # refresh the whole corpus
    python -m mf_faq.ingest --scheme mo_elss      # one scheme (repeatable)
    python -m mf_faq.ingest --all --dry-run       # fetch and compare, write nothing
    python -m mf_faq.ingest --all --json          # machine-readable, for P6
    python -m mf_faq.ingest --all --no-index      # registry only, skip embedding
    python -m mf_faq.ingest --all --rebuild-index # drop the collection, re-embed all

Also installed as `mf-faq-ingest` (see `[project.scripts]`).

**A successful run updates the vector index too (P3).** Registry first, index
second — see `retrieval/indexer.py` for why that order is the survivable one,
and why a sync failure still fails the run. `--no-index` skips it for
environments without the embedding stack; the next indexed run repairs the gap
rather than inheriting it, because `store.sync()` reconciles against the index's
real contents.

**There is no default target.** A bare invocation prints usage and exits 2
rather than crawling five live pages, because the obvious accident here is
someone exploring the CLI and putting real traffic on groww.in to find out what
it does. `--all` is nine keystrokes and makes the intent explicit.

**The exit code is the contract**, not the output. 0 means every scheme parsed
and every fact was accepted; 1 means at least one scheme failed or at least one
update was rejected as an I-09 conflict. P6 hangs the whole publish decision off
this: a non-zero exit skips the commit step, so the repository keeps the last
good index (ARCH §8.4). 2 is reserved for usage errors, which are the operator's
problem rather than the corpus's.

This module is deliberately thin. Everything it knows how to do lives in
`pipeline.run()`; the CLI only parses arguments, turns on logging, and chooses a
rendering. Anything more interesting belongs one layer down where it is testable
without argv.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from mf_faq.ingest.pipeline import RunReport, format_report, run
from mf_faq.logging_setup import configure_logging, get_logger
from mf_faq.settings import get_settings, get_sources

if TYPE_CHECKING:  # `SyncReport` lives behind the torch-heavy retrieval import
    from mf_faq.retrieval.store import SyncReport

log = get_logger(__name__)

EXIT_USAGE = 2


def build_parser(scheme_ids: list[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mf_faq.ingest",
        description="Refresh the mutual fund fact registry from groww.in.",
        epilog="Facts-only. No investment advice.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--all",
        action="store_true",
        help="refresh every scheme in config/sources.yaml",
    )
    target.add_argument(
        "--scheme",
        action="append",
        metavar="ID",
        choices=scheme_ids,
        help=f"refresh one scheme; repeatable. One of: {', '.join(scheme_ids)}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and compare, but write nothing — shows what a real run would do",
    )
    parser.add_argument("--db", type=Path, default=None, metavar="PATH", help="registry.db path")
    parser.add_argument(
        "--chroma-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="vector index directory (pairs with --db; both default to config)",
    )
    index = parser.add_mutually_exclusive_group()
    index.add_argument(
        "--no-index",
        action="store_true",
        help="update the registry but skip the vector index (no embedding model needed)",
    )
    index.add_argument(
        "--rebuild-index",
        action="store_true",
        help="drop the collection and re-embed the whole corpus (recovery, not routine)",
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR")
    return parser


def report_as_dict(report: RunReport, index_sync: SyncReport | None = None) -> dict:
    """Stable machine-readable shape. P6 parses this; keep the keys steady."""
    return {
        "run_id": report.run_id,
        "status": report.status,
        "exit_code": report.exit_code,
        "dry_run": report.dry_run,
        "committed": report.committed,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "requests_made": report.requests_made,
        "cache_hits": report.cache_hits,
        "counts": {
            **report.run_row(),
            "new": report.summary.new,
            "changed": report.summary.changed,
            "refreshed": report.summary.refreshed,
            "unchanged": report.summary.unchanged,
            "conflicts": len(report.conflicts),
        },
        "index": index_sync.as_dict() if index_sync else None,
        "to_embed": [c.doc_id for c in report.cards_to_embed()],
        "to_restamp": [c.doc_id for c in report.cards_to_restamp()],
        "schemes": [
            {
                "scheme_id": s.scheme_id,
                "status": s.status,
                "facts": len(s.cards),
                "requested": s.requested,
                "missing": len(s.missing),
                "error": s.error,
            }
            for s in report.schemes
        ],
        "conflicts": [{"doc_id": d.doc_id, "reason": d.reason} for d in report.conflicts],
        "missing": [asdict(m) for m in report.missing],
    }


def main(argv: list[str] | None = None) -> int:
    sources = get_sources()
    parser = build_parser([s.scheme_id for s in sources.schemes])
    args = parser.parse_args(argv)

    if not args.all and not args.scheme:
        parser.print_usage(sys.stderr)
        print(
            "\nerror: choose a target — --all for the whole corpus, or "
            "--scheme ID for one.\nRefusing to crawl groww.in by default.",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # Logs go to stderr (see `configure_logging`), so `--json` needs no special
    # quieting — stdout carries the report and nothing else at any log level.
    configure_logging(args.log_level or get_settings().log_level)

    report = run(
        args.scheme,  # None when --all, which pipeline.run() reads as "everything"
        db_path=args.db,
        sources=sources,
        dry_run=args.dry_run,
    )

    index_sync = None
    index_error = None
    if _should_index(args, report):
        try:
            # Imported here, not at module scope: `--dry-run` and `--no-index`
            # must not pay for torch, and neither must `--help`.
            from mf_faq.retrieval.indexer import rebuild, sync_from_run
            from mf_faq.retrieval.store import VectorStore

            # `--db` without a matching `--chroma-dir` would let an operator
            # redirect the registry to a scratch file and still overwrite the
            # live index. The two paths move together or not at all.
            store = VectorStore.open(path=args.chroma_dir) if args.chroma_dir else None
            do = rebuild if args.rebuild_index else sync_from_run
            index_sync = do(report, db_path=args.db, store=store)
        except Exception as exc:  # noqa: BLE001 - reported, then exits non-zero
            # The registry has moved and the index has not. The next run repairs
            # it (store.sync is convergent), but this run must not publish.
            index_error = f"{type(exc).__name__}: {exc}"
            log.exception("index sync failed — registry is ahead of the index")

    if args.json:
        print(json.dumps(report_as_dict(report, index_sync), indent=2))
    else:
        print(format_report(report))
        print(_format_index(index_sync, index_error, args))

    return 1 if index_error else report.exit_code


def _should_index(args: argparse.Namespace, report: RunReport) -> bool:
    """Index only a real run that actually wrote the registry.

    Each condition maps to a rule in `retrieval/indexer.py`: a dry run wrote
    nothing, a failed run wrote nothing, and `--no-index` was asked to skip.
    """
    return not (args.no_index or args.dry_run or report.failed_schemes)


def _format_index(sync: SyncReport | None, error: str | None, args: argparse.Namespace) -> str:
    if error:
        return (
            f"  INDEX FAILED — {error}\n  registry is ahead of the index; the next run repairs it"
        )
    if sync is None:
        if args.no_index:
            return "  index: skipped (--no-index)"
        if args.dry_run:
            return "  index: not written (dry run)"
        return "  index: not written (run failed)"
    line = (
        f"  index: {sync.embedded} embedded, {sync.restamped} restamped, "
        f"{sync.unchanged} unchanged, {sync.deleted} deleted"
    )
    if sync.repaired:
        line += f"\n  INDEX DRIFT repaired {sync.repaired} chunk(s): {sync.repaired_ids[:5]}"
    return line
