"""The only fact parser (P2.4 + P2.5).

One extractor per in-scope fact. Each returns a value **and** its `source_as_of`,
because a fact without a date is uncitable and therefore unusable (ARCH §11) —
the footer and the freshness gate both depend on it.

Two things here are easy to get wrong and expensive to get wrong:

**Timezone.** `holdings[].portfolio_date` arrives as `2026-07-30T18:30:00.000Z`.
That is 18:30 UTC = **00:00 IST on 31 July** — the month-end disclosure date.
Reading the date naively yields 30 July, silently off by one and landing on the
wrong month-end. Timezone-aware values are converted to IST; naive values
(`2026-08-28T00:00:00`) are already IST-local and must NOT be shifted.

**Scheme identity.** A silent redirect would ingest one fund's data under
another's `scheme_id` (edge case I-06, high severity). Every parse verifies the
payload's `search_id` against the URL slug before extracting anything.

No `riskometer` extractor exists. The plan text lists one, but P0.4 declared it
out of corpus: `nfo_risk` and `return_stats[].risk` disagree for the same fund
and neither is dated. Riskometer queries refuse (eval `R-oob-riskometer`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from mf_faq.logging_setup import get_logger

log = get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# Values Groww uses for "nothing here". A fact carrying one of these is absent,
# not empty — storing it would produce an answer stating a placeholder (I-03).
NULL_MARKERS = frozenset({"", "-", "--", "n/a", "na", "nil-", "none", "null", "not available"})


class ParseError(RuntimeError):
    """The payload could not be parsed at all — a structural problem."""


class SchemeIdentityMismatch(ParseError):
    """The payload describes a different scheme than requested (I-06)."""


class FactUnavailable(Exception):
    """This one fact is missing, undated, or unusable.

    Not an error: an absent fact is a coverage gap that refuses at query time
    (PS §5.1). The run continues; other facts on the page are unaffected.
    """

    def __init__(self, doc_type: str, reason: str) -> None:
        self.doc_type = doc_type
        self.reason = reason
        super().__init__(f"{doc_type}: {reason}")


@dataclass(frozen=True)
class ExtractedFact:
    scheme_id: str
    doc_type: str
    value: Any
    source_as_of: date
    source_url: str
    date_is_page_level: bool = False

    @property
    def doc_id(self) -> str:
        return f"{self.scheme_id}:{self.doc_type}"


# --------------------------------------------------------------------------
# Date handling
# --------------------------------------------------------------------------


def parse_iso_date(raw: str) -> date:
    """Parse an ISO timestamp to an IST calendar date.

    Timezone-aware values are converted to IST first. `2026-07-30T18:30:00Z`
    is 00:00 IST on 31 July — the month-end portfolio date, not 30 July.
    """
    text = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unparseable ISO date {raw!r}") from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST)
    return dt.date()


def parse_display_date(raw: str) -> date:
    """Parse Groww's display format, e.g. `28-Aug-2026` (used by `nav_date`)."""
    try:
        return datetime.strptime(raw.strip(), "%d-%b-%Y").date()
    except ValueError as exc:
        raise ValueError(f"unparseable display date {raw!r}") from exc


def _today_ist() -> date:
    return datetime.now(UTC).astimezone(IST).date()


def _validated_date(doc_type: str, value: date, *, today: date | None = None) -> date:
    """Reject dates from the future — a parser bug, never real data (F-04)."""
    today = today or _today_ist()
    if value > today:
        raise FactUnavailable(
            doc_type, f"source_as_of {value.isoformat()} is in the future (today IST {today})"
        )
    return value


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip().casefold() in NULL_MARKERS


def _require(payload: dict, key: str, doc_type: str) -> Any:
    value = payload.get(key)
    if _is_null(value):
        raise FactUnavailable(doc_type, f"'{key}' is absent or empty")
    return value


def _page_date(payload: dict, doc_type: str) -> date:
    """`nav_date` — the page-level date used by undated facts (PS §9).

    Accepted deliberately: this is the page's currency date, not these facts'
    publication date. It is why min_sip / lock_in / benchmark are exempt from
    the freshness gate, and why it must stay out of fact-card text.
    """
    raw = _require(payload, "nav_date", doc_type)
    try:
        return parse_display_date(str(raw))
    except ValueError as exc:
        raise FactUnavailable(doc_type, str(exc)) from exc


def _latest_dated(rows: list[dict], date_key: str, doc_type: str) -> dict:
    """Pick the most recent entry from a dated history list."""
    dated = [r for r in rows if isinstance(r, dict) and not _is_null(r.get(date_key))]
    if not dated:
        raise FactUnavailable(doc_type, f"no entry carries '{date_key}'")
    return max(dated, key=lambda r: str(r[date_key]))


# --------------------------------------------------------------------------
# Extractors — one per in-scope fact
# --------------------------------------------------------------------------


def extract_nav(payload: dict) -> tuple[Any, date, bool]:
    value = _require(payload, "nav", "nav")
    raw_date = _require(payload, "nav_date", "nav")
    try:
        return float(value), parse_display_date(str(raw_date)), False
    except (TypeError, ValueError) as exc:
        raise FactUnavailable("nav", str(exc)) from exc


def extract_expense_ratio(payload: dict) -> tuple[Any, date, bool]:
    value = _require(payload, "expense_ratio", "expense_ratio")
    history = payload.get("historic_fund_expense") or []
    if not history:
        raise FactUnavailable("expense_ratio", "no historic_fund_expense to date the value")
    latest = _latest_dated(history, "as_on_date", "expense_ratio")
    try:
        return float(value), parse_iso_date(str(latest["as_on_date"])), False
    except (TypeError, ValueError) as exc:
        raise FactUnavailable("expense_ratio", str(exc)) from exc


def extract_exit_load(payload: dict) -> tuple[Any, date, bool]:
    # "Nil" is a real, meaningful exit load (the ELSS fund has one), so it must
    # not be treated as a null marker.
    value = payload.get("exit_load")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise FactUnavailable("exit_load", "'exit_load' is absent or empty")
    history = payload.get("historic_exit_loads") or []
    if not history:
        raise FactUnavailable("exit_load", "no historic_exit_loads to date the value")
    latest = _latest_dated(history, "as_on_date", "exit_load")
    try:
        return str(value).strip(), parse_iso_date(str(latest["as_on_date"])), False
    except ValueError as exc:
        raise FactUnavailable("exit_load", str(exc)) from exc


def extract_holdings(payload: dict) -> tuple[Any, date, bool]:
    """Verbatim disclosure only (PS §8.2) — name, weight, date. No commentary.

    Returned as ONE value, not one per holding: with 32-52 holdings and top_k=4,
    per-holding chunks would truncate a "top holdings" answer into a confident
    but incomplete list (edge case R-08).
    """
    rows = payload.get("holdings") or []
    if not rows:
        raise FactUnavailable("holdings", "'holdings' is absent or empty")

    dated = [r for r in rows if not _is_null(r.get("portfolio_date"))]
    if not dated:
        raise FactUnavailable("holdings", "no holding carries 'portfolio_date'")

    try:
        as_of = max(parse_iso_date(str(r["portfolio_date"])) for r in dated)
    except ValueError as exc:
        raise FactUnavailable("holdings", str(exc)) from exc

    entries = [
        {"company_name": str(r["company_name"]).strip(), "corpus_per": float(r["corpus_per"])}
        for r in rows
        if not _is_null(r.get("company_name")) and not _is_null(r.get("corpus_per"))
    ]
    if not entries:
        raise FactUnavailable("holdings", "no holding has both a name and a weight")

    entries.sort(key=lambda e: (-e["corpus_per"], e["company_name"]))
    return {"holdings": entries, "count": len(entries)}, as_of, False


def extract_min_sip(payload: dict) -> tuple[Any, date, bool]:
    value = _require(payload, "min_sip_investment", "min_sip")
    try:
        amount = int(value)
    except (TypeError, ValueError) as exc:
        raise FactUnavailable("min_sip", f"non-numeric min_sip_investment {value!r}") from exc
    if amount <= 0:
        raise FactUnavailable("min_sip", f"implausible min SIP {amount}")
    return amount, _page_date(payload, "min_sip"), True


def extract_lock_in(payload: dict) -> tuple[Any, date, bool]:
    """Lock-in period, or an explicit "none".

    An all-null `lock_in` means the scheme has NO lock-in — a real, answerable
    fact, not a missing one. Treating it as absent would make the assistant
    refuse "is there a lock-in on the Next 50 fund?", where the correct answer
    is "no" (golden case G-lockin-next50-none).
    """
    raw = payload.get("lock_in")
    if raw is None:
        raise FactUnavailable("lock_in", "'lock_in' key absent")
    if not isinstance(raw, dict):
        raise FactUnavailable("lock_in", f"unexpected lock_in shape {type(raw).__name__}")

    parts = {k: raw.get(k) for k in ("years", "months", "days")}
    if all(_is_null(v) or int(v) == 0 for v in parts.values()):
        value: dict[str, Any] = {"has_lock_in": False}
    else:
        value = {
            "has_lock_in": True,
            "years": int(parts["years"] or 0),
            "months": int(parts["months"] or 0),
            "days": int(parts["days"] or 0),
        }
    return value, _page_date(payload, "lock_in"), True


def extract_benchmark(payload: dict) -> tuple[Any, date, bool]:
    value = _require(payload, "benchmark", "benchmark")
    return str(value).strip(), _page_date(payload, "benchmark"), True


EXTRACTORS: dict[str, Callable[[dict], tuple[Any, date, bool]]] = {
    "nav": extract_nav,
    "expense_ratio": extract_expense_ratio,
    "exit_load": extract_exit_load,
    "holdings": extract_holdings,
    "min_sip": extract_min_sip,
    "lock_in": extract_lock_in,
    "benchmark": extract_benchmark,
    # No "riskometer": out of corpus per P0.4.
}


# --------------------------------------------------------------------------
# Identity + orchestration
# --------------------------------------------------------------------------


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def verify_scheme_identity(payload: dict, expected_url: str) -> None:
    """Confirm the payload describes the scheme we asked for (I-06).

    `search_id` tracks the URL slug even for the renamed ELSS fund, so it is a
    reliable anchor. Without this check a redirect would file one fund's NAV
    under another's `scheme_id`, and every downstream guardrail would pass it.
    """
    search_id = payload.get("search_id")
    if _is_null(search_id):
        raise SchemeIdentityMismatch("payload has no 'search_id' — cannot verify identity")
    if str(search_id).strip() != _slug(expected_url):
        raise SchemeIdentityMismatch(
            f"payload is for '{search_id}' but '{_slug(expected_url)}' was requested — "
            "likely a redirect; refusing to file this data under the wrong scheme"
        )


def parse_facts(
    payload: dict,
    *,
    scheme_id: str,
    source_url: str,
    doc_types: list[str] | tuple[str, ...],
    today: date | None = None,
) -> tuple[list[ExtractedFact], list[FactUnavailable]]:
    """Extract the requested facts. Returns (facts, per-fact failures).

    Verifies scheme identity first — a mismatch raises, because filing wrong
    data is worse than filing none. Individual fact failures are collected, not
    raised: one missing fact is a coverage gap, not a run failure.
    """
    verify_scheme_identity(payload, source_url)

    facts: list[ExtractedFact] = []
    failures: list[FactUnavailable] = []

    for doc_type in doc_types:
        extractor = EXTRACTORS.get(doc_type)
        if extractor is None:
            failures.append(FactUnavailable(doc_type, "no extractor (out of corpus?)"))
            continue
        try:
            value, as_of, page_level = extractor(payload)
            as_of = _validated_date(doc_type, as_of, today=today)
        except FactUnavailable as exc:
            log.warning(
                "fact rejected",
                extra={"scheme_id": scheme_id, "doc_type": doc_type, "reason": exc.reason},
            )
            failures.append(exc)
            continue
        facts.append(
            ExtractedFact(
                scheme_id=scheme_id,
                doc_type=doc_type,
                value=value,
                source_as_of=as_of,
                source_url=source_url,
                date_is_page_level=page_level,
            )
        )

    return facts, failures


def parse_scheme_page(
    html: str,
    *,
    scheme_id: str,
    source_url: str,
    doc_types: list[str] | tuple[str, ...],
    today: date | None = None,
) -> tuple[list[ExtractedFact], list[FactUnavailable]]:
    """Convenience: normalise raw HTML, then extract."""
    from mf_faq.ingest.normalise import normalise

    page = normalise(html)
    return parse_facts(
        page.payload,
        scheme_id=scheme_id,
        source_url=source_url,
        doc_types=doc_types,
        today=today,
    )
