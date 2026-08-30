"""Preview extracted facts without persisting anything.

    python -m mf_faq.ingest.preview                 # from saved fixtures (offline)
    python -m mf_faq.ingest.preview --live          # fetch groww.in now
    python -m mf_faq.ingest.preview --scheme mo_elss --json

A development tool, not part of the pipeline: it shows the *parsed values*,
with no change detection and no registry. For what a run would actually do to
the registry, use `python -m mf_faq.ingest --all --dry-run` (P2.9). This stays
because it is the only view that runs entirely from saved fixtures, costing no
requests at all. Strictly read-only — it never touches the registry or index.

Defaults to the Phase 0 fixtures so it costs no requests against groww.in.
"""

from __future__ import annotations

import argparse
import json
import sys

from mf_faq.ingest.parse import parse_facts
from mf_faq.logging_setup import configure_logging
from mf_faq.settings import REPO_ROOT, get_sources

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "groww"


def load_fixture(scheme_id: str) -> dict:
    path = FIXTURES / f"{scheme_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no fixture for {scheme_id}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_live(scheme_ids: list[str]) -> dict[str, dict]:
    from mf_faq.ingest.fetch import build_fetcher
    from mf_faq.ingest.normalise import normalise

    sources = get_sources()
    out: dict[str, dict] = {}
    with build_fetcher(sources) as fetcher:
        for scheme_id in scheme_ids:
            scheme = sources.scheme(scheme_id)
            page = fetcher.fetch(str(scheme.url))
            out[scheme_id] = normalise(page.body).payload
    return out


def render(fact) -> str:
    """One-line human rendering. Not the fact card — that is 2.6."""
    value = fact.value
    if fact.doc_type == "holdings":
        top = value["holdings"][0]
        return f"{value['count']} holdings, largest {top['company_name']} @ {top['corpus_per']}%"
    if fact.doc_type == "lock_in":
        if not value.get("has_lock_in"):
            return "no lock-in"
        return f"{value['years']}y {value['months']}m {value['days']}d"
    if fact.doc_type == "expense_ratio":
        return f"{value}%"
    if fact.doc_type == "min_sip":
        return f"Rs {value}"
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--scheme", help="one scheme_id (default: all)")
    parser.add_argument("--live", action="store_true", help="fetch groww.in instead of fixtures")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    configure_logging("WARNING")
    sources = get_sources()
    scheme_ids = [args.scheme] if args.scheme else [s.scheme_id for s in sources.schemes]

    try:
        payloads = (
            load_live(scheme_ids) if args.live else {sid: load_fixture(sid) for sid in scheme_ids}
        )
    except (FileNotFoundError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    origin = "LIVE groww.in" if args.live else f"fixtures ({FIXTURES.relative_to(REPO_ROOT)})"
    records, total_missing = [], 0

    for scheme_id in scheme_ids:
        scheme = sources.scheme(scheme_id)
        facts, failures = parse_facts(
            payloads[scheme_id],
            scheme_id=scheme_id,
            source_url=str(scheme.url),
            doc_types=scheme.extract,
        )
        total_missing += len(failures)
        for fact in sorted(facts, key=lambda f: f.doc_type):
            records.append(
                {
                    "doc_id": fact.doc_id,
                    "scheme_id": fact.scheme_id,
                    "doc_type": fact.doc_type,
                    "value": fact.value,
                    "display": render(fact),
                    "source_as_of": fact.source_as_of.isoformat(),
                    "date_is_page_level": fact.date_is_page_level,
                    "source_url": fact.source_url,
                }
            )
        for failure in failures:
            records.append(
                {
                    "doc_id": f"{scheme_id}:{failure.doc_type}",
                    "scheme_id": scheme_id,
                    "doc_type": failure.doc_type,
                    "unavailable": failure.reason,
                }
            )

    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return 0

    print(f"Source: {origin}")
    print("NOT persisted — use `python -m mf_faq.ingest` to write the registry.\n")

    current = None
    for record in records:
        if record["scheme_id"] != current:
            current = record["scheme_id"]
            print(f"\n{sources.scheme(current).display_name}")
            print(f"  {sources.scheme(current).url}")
        if "unavailable" in record:
            print(f"    {record['doc_type']:15} UNAVAILABLE — {record['unavailable']}")
        else:
            flag = "  (page-level date)" if record["date_is_page_level"] else ""
            print(
                f"    {record['doc_type']:15} {record['display'][:46]:48}"
                f"as_of {record['source_as_of']}{flag}"
            )

    shown = sum(1 for r in records if "unavailable" not in r)
    print(f"\n{shown} facts, {total_missing} unavailable, {len(scheme_ids)} scheme(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
