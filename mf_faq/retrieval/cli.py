"""Inspect retrieval for one query: `python -m mf_faq.retrieval "..."`.

The retrieval-side sibling of `ingest/preview.py`, and it answers the question
that file cannot: *given this wording, what would the model actually be shown?*

Read-only. It opens the committed index, runs one query, and prints the
resolution, the stripped query text and every candidate chunk with its score.
Nothing is written, and no LLM is called — so it costs nothing against the Groq
budget and is safe to run against a live index.

It exists mainly for the tuning P5.5 has to do. The similarity floor turned out
not to separate answerable questions from refusable ones (measured in
`tests/test_retrieval_eval.py`), so choosing its value means looking at real
scores for real wordings. `--no-floor` shows what the floor is suppressing;
`--floor` tries a candidate value without editing config.
"""

from __future__ import annotations

import argparse
import json
import sys

from mf_faq.logging_setup import configure_logging
from mf_faq.settings import get_settings

DISCLAIMER = "Facts-only. No investment advice."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mf_faq.retrieval",
        description="Show what retrieval returns for a query. Read-only; no LLM call.",
        epilog=DISCLAIMER,
    )
    parser.add_argument("query", nargs="?", help="the question to retrieve for")
    parser.add_argument("--top-k", type=int, default=None, help="override top_k")
    parser.add_argument("--floor", type=float, default=None, help="override the similarity floor")
    parser.add_argument(
        "--no-floor",
        action="store_true",
        help="disable the floor — shows the scores it would suppress",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--log-level", default="WARNING", help="DEBUG / INFO / WARNING / ERROR")
    return parser


def _as_dict(result) -> dict:
    return {
        "outcome": result.outcome.value,
        "scheme_id": result.scheme_id,
        "candidates": list(result.candidates),
        "reason": result.scheme.reason if result.scheme else None,
        "floor": result.floor,
        "chunks": [
            {
                "doc_id": c.doc_id,
                "doc_type": c.doc_type,
                "similarity": round(c.similarity, 4),
                "source_as_of": c.source_as_of,
                "source_url": c.source_url,
                "text": c.text,
            }
            for c in result.chunks
        ],
        "discarded": [
            {"doc_id": c.doc_id, "similarity": round(c.similarity, 4)} for c in result.discarded
        ],
    }


def _format(result, query: str, stripped: str) -> str:
    lines = [
        f"query:    {query!r}",
        f"embedded: {stripped!r}" + (" (unchanged)" if stripped == query else ""),
        f"outcome:  {result.outcome.value}",
    ]
    if result.scheme:
        lines.append(f"scheme:   {result.scheme_id or list(result.candidates)}")
        lines.append(f"reason:   {result.scheme.reason}")
    lines.append(f"floor:    {result.floor}")
    lines.append("")
    if not result.chunks:
        lines.append("  no chunks returned")
    for i, c in enumerate(result.chunks, start=1):
        lines.append(f"  [{i}] {c.similarity:.4f}  {c.doc_type:14} as of {c.source_as_of}")
        lines.append(f"      {c.text[:160]}{'...' if len(c.text) > 160 else ''}")
    for c in result.discarded:
        lines.append(f"  ( ) {c.similarity:.4f}  {c.doc_type:14} BELOW FLOOR — not returned")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.query or not args.query.strip():
        parser.print_usage(sys.stderr)
        print("\nerror: a query is required.", file=sys.stderr)
        return 2

    configure_logging(args.log_level)

    # Imported here so `--help` does not load torch.
    from mf_faq.retrieval.search import build_searcher

    floor = -1.0 if args.no_floor else args.floor
    searcher = build_searcher(top_k=args.top_k, similarity_floor=floor)

    if searcher.store.count() == 0:
        print(
            f"index at {get_settings().chroma_dir} is empty — "
            "run `python -m mf_faq.ingest --all` first",
            file=sys.stderr,
        )
        return 1

    result = searcher.search(args.query)
    stripped = (
        searcher.resolver.strip_scheme_terms(args.query, result.scheme_id)
        if result.scheme_id
        else args.query
    )

    if args.json:
        print(json.dumps(_as_dict(result), indent=2))
    else:
        print(_format(result, args.query, stripped))
    return 0
