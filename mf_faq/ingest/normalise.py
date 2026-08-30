"""Normalise a fetched page into a stable, hashable form (P2.3, ARCH §8.3).

**This deviates from the literal task text, and the reason matters.** Task 2.3
was written expecting HTML scraping ("strip scripts/styles/banners, extract main
content"). P0.1 found that Groww embeds every fact in a typed `__NEXT_DATA__`
JSON payload, so "normalise before hashing" here means *extract the payload and
canonicalise it* rather than *clean up HTML text*.

Measured on two fetches of the same page 4 seconds apart:

    raw HTML          hashes DIFFER  (identical 413,184-byte length)
    mfServerSideData  hashes MATCH   (0 differing keys)

The whole of the HTML difference was 52 characters in one Cloudflare
`data-cfemail` attribute, which is re-obfuscated on **every** response. (The
`nonce` on the script tag also rotates, though on a slower cycle — it was
unchanged across those two fetches but differed an hour earlier.)

The consequence is the point of this module: **hashing raw HTML would report
"changed" on every single fetch**, re-embedding the whole corpus daily and
destroying the cost argument for the daily cadence (ARCH §8.3). Stripping the
nonce alone would not save you — `data-cfemail` guarantees a fresh hash anyway.
Hash the extracted payload.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from mf_faq.logging_setup import get_logger

log = get_logger(__name__)

# Tolerant of attribute order and of the nonce/crossorigin attributes, which
# vary. Anchoring on `id="__NEXT_DATA__"` alone is what keeps this stable.
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)

PAYLOAD_PATH = ("props", "pageProps", "mfServerSideData")

# Fields PS §5.3 bars, stripped as defence in depth for the P0.4 field allowlist.
# The allowlist in sources.yaml is the primary control; this is the second line,
# so that even code which mishandles the whole payload cannot leak star ratings
# or return comparisons into the index, where they would read as authoritative
# retrieved context.
BARRED_FIELDS = frozenset(
    {
        "groww_rating",  # a star rating — recommendation-adjacent
        "analysis",  # PROS/CONS: "Consistently higher annualised returns..."
        "peerComparison",  # cross-scheme comparison
        "simple_return",  # performance
        "sip_return",  # performance
        "return_stats",  # performance + a risk_rating that contradicts nfo_risk
        "category_info",  # carries tax_impact guidance (advisory-adjacent)
        "fund_news",
        "actions",
        "primary_action",
    }
)


class NormaliseError(RuntimeError):
    """The page did not yield a usable payload.

    Raised loudly rather than returning None: a missing payload means Groww
    changed its page structure, which breaks all five schemes at once
    (ARCH §15.7) and needs a human, not a silent skip.
    """


@dataclass(frozen=True)
class NormalisedPage:
    """A page reduced to its stable, barred-field-free payload."""

    payload: dict[str, Any]
    barred_removed: tuple[str, ...]
    raw_bytes: int

    def project(self, keys: list[str] | tuple[str, ...]) -> dict[str, Any]:
        """Keep only the named payload keys. Missing keys are simply absent."""
        return {k: self.payload[k] for k in keys if k in self.payload}

    def fingerprint(self) -> str:
        """Hash of the whole normalised payload — diagnostics only.

        Never use this for change detection: it flips whenever *any* fact on the
        page moves, so NAV's daily change would mark every other fact as changed
        too. Per-fact hashing (2.7) is the mechanism that actually matters.
        """
        return content_hash(self.payload)


def extract_payload(html: str) -> dict[str, Any]:
    """Pull `mfServerSideData` out of a Groww scheme page."""
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        raise NormaliseError(
            "no __NEXT_DATA__ script found — Groww page structure has changed. "
            "This breaks all five schemes at once; the parser needs updating."
        )

    try:
        blob = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise NormaliseError(f"__NEXT_DATA__ is not valid JSON: {exc}") from exc

    node: Any = blob
    for key in PAYLOAD_PATH:
        if not isinstance(node, dict) or key not in node:
            raise NormaliseError(
                f"payload path {'.'.join(PAYLOAD_PATH)} missing at '{key}' — "
                "Groww's page-props shape has changed."
            )
        node = node[key]

    if not isinstance(node, dict) or not node:
        raise NormaliseError(f"{'.'.join(PAYLOAD_PATH)} is empty or not an object")

    return node


def strip_barred(payload: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Remove PS §5.3-barred fields. Returns (clean_payload, removed_keys)."""
    removed = tuple(sorted(k for k in payload if k in BARRED_FIELDS))
    clean = {k: v for k, v in payload.items() if k not in BARRED_FIELDS}
    return clean, removed


def canonical(value: Any) -> Any:
    """Recursively canonicalise for stable serialisation.

    Lists of objects are sorted by their own canonical form. For every fact in
    this corpus the *content* is the fact and the ordering is not — a holdings
    list re-ordered upstream with identical companies and weights is not a
    change, and must not trigger a re-embed.
    """
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        items = [canonical(v) for v in value]
        try:
            return sorted(items, key=lambda i: json.dumps(i, sort_keys=True, default=str))
        except TypeError:  # pragma: no cover - defensive
            return items
    return value


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, sorted lists, no incidental whitespace."""
    return json.dumps(
        canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def content_hash(value: Any) -> str:
    """SHA-256 of the canonical form. This is what `documents.content_hash` stores."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalise(html: str) -> NormalisedPage:
    """HTML → stable payload with barred fields removed."""
    payload = extract_payload(html)
    clean, removed = strip_barred(payload)
    if removed:
        log.debug("stripped barred fields", extra={"fields": list(removed)})
    return NormalisedPage(
        payload=clean,
        barred_removed=removed,
        raw_bytes=len(html.encode("utf-8")),
    )
