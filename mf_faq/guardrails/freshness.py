"""The `max_age` gate on `source_as_of` (P4.3, ARCH §7.3).

Runs after retrieval, before generation — there is no point spending a Groq
call on a document that cannot legally be quoted once its age is known. Every
policy value here comes from `config/sources.yaml`'s `doc_types` block, which
P0.9 and P1.2 already turned into the single source of truth (`past_max_age`:
refuse / flag / exempt, `max_age_days`, `unit`: days / business_days). This
module does not repeat those numbers; it evaluates them.

**Measured against `source_as_of`, never `fetched_at`** (PS §4.5 behaviour 5,
ARCH §7.3). `fetched_at` says a scheduler run touched the page today; it says
nothing about whether the page's own content changed. Gating on it would let a
daily no-op run launder a stale document as current — precisely the failure
PS §4.5 exists to prevent, and precisely the distinction `db.py`'s schema
comment calls out as "the point of the whole schema."

Business-day counting, and why it needs no holiday calendar here
------------------------------------------------------------------
`nav` is the one `unit: business_days` policy (`max_age_days: 1`). A NAV from
Friday must still read as fresh on Monday morning — the weekend is not a gap in
publication, it is Saturday and Sunday having no NAV to publish. Counting only
Mon–Fri handles that correctly without a market-holiday calendar: `business_days_elapsed`
counts calendar days strictly after `source_as_of` up to and including today
that fall Mon–Fri, so a Friday-to-Monday span reads as 1 elapsed business day —
within the 1-day floor — while a Thursday-to-Monday span (2 elapsed business
days, meaning a Friday NAV was missed) correctly exceeds it and refuses (F-08).

A **market** holiday (Diwali, etc.) still leaves NAV genuinely stale for longer
than this gate can see, because this gate only knows about weekends. That gap
is deliberately not closed here: ARCH §8.5's missing-update check is where a
real holiday calendar belongs (P6.6), because that check runs against the whole
corpus once a day and can afford the calendar lookup; this gate runs on every
retrieved chunk of every query and must stay pure date arithmetic. F-01/F-02's
*phrasing* requirement — never implying a stale NAV is "current" — is a
separate, unconditional template (P0.9, rendered in `generation/render.py`);
this gate decides refuse-vs-flag-vs-fresh, not what the sentence says.

**The boundary is inclusive**, consistent with the P3 similarity floor (R-02):
`age <= max_age_days` is fresh; only `age > max_age_days` trips the policy.
Tested explicitly on both sides (F-03).

**Timezone is pinned to IST** (F-07). The scheduler commits at 18:00 UTC —
23:30 IST — and every `source_as_of` in the registry is an IST-local date
(P2.5). Comparing "today" in UTC would shift the boundary by a day for several
hours around midnight IST; `evaluate_freshness` always resolves "today" in IST.

**A future `source_as_of` always refuses**, overriding even an EXEMPT policy
(F-04). P2.5 already rejects a future-dated fact at ingest — this is defence in
depth, not the primary control — but confidently serving a footer date that has
not happened yet breaks the "footer date provably reflects source" guarantee
more completely than ordinary staleness does, so it is not something even an
exempt fact type should be allowed to paper over.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from enum import StrEnum

from mf_faq.schemas import SourcesConfig, StalePolicy
from mf_faq.settings import get_sources

#: India Standard Time, fixed offset — India observes no daylight saving.
IST = timezone(timedelta(hours=5, minutes=30))

_BUSINESS_WEEKDAYS = range(5)  # Monday=0 .. Friday=4


class FreshnessVerdict(StrEnum):
    FRESH = "fresh"
    FLAG = "flag"
    REFUSE = "refuse"


@dataclass(frozen=True)
class FreshnessCheck:
    """The gate's verdict for one fact, with the evidence behind it."""

    verdict: FreshnessVerdict
    doc_type: str
    source_as_of: date
    age: int
    unit: str
    max_age_days: int | None
    policy: StalePolicy

    @property
    def is_future_dated(self) -> bool:
        """A F-04 anomaly: overrides even an exempt policy. See module docstring."""
        return self.age < 0


def business_days_elapsed(start: date, end: date) -> int:
    """Business days (Mon–Fri) strictly after `start`, up to and including `end`.

    `start == end` is 0 elapsed. A weekend does not add to the count, so a
    Friday `source_as_of` reads as 1 elapsed business day on the following
    Monday — the correct answer for a fact whose publication genuinely skips
    weekends, not a 3-day gap. See the module docstring for what this does and
    does not cover (weekends yes, market holidays no).
    """
    if end <= start:
        return 0
    count = 0
    day = start
    for _ in range((end - start).days):
        day += timedelta(days=1)
        if day.weekday() in _BUSINESS_WEEKDAYS:
            count += 1
    return count


def _today(now: datetime | None) -> date:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("evaluate_freshness needs a timezone-aware `now` — see F-07")
    return moment.astimezone(IST).date()


def evaluate_freshness(
    doc_type: str,
    source_as_of: date,
    *,
    now: datetime | None = None,
    sources: SourcesConfig | None = None,
) -> FreshnessCheck:
    """Evaluate one fact's freshness against its `doc_types` policy.

    `now` defaults to the current moment; tests pass a fixed one so results are
    deterministic. Always resolved to an IST calendar date before comparison.
    """
    sources = sources or get_sources()
    policy = sources.doc_types[doc_type]  # KeyError on an unknown doc_type — fail loud
    today = _today(now)
    calendar_age = (today - source_as_of).days

    if calendar_age < 0:
        # F-04: a future date. Refuses regardless of policy, exempt included.
        return FreshnessCheck(
            FreshnessVerdict.REFUSE,
            doc_type,
            source_as_of,
            age=calendar_age,
            unit=policy.unit,
            max_age_days=policy.max_age_days,
            policy=policy.past_max_age,
        )

    if policy.past_max_age is StalePolicy.EXEMPT:
        # PS §9 item 9: min_sip / lock_in / benchmark carry nav_date as their
        # source_as_of, which advances daily regardless of value — the gate
        # can never legitimately trip for them (schemas.py enforces
        # max_age_days is null whenever past_max_age is exempt).
        return FreshnessCheck(
            FreshnessVerdict.FRESH,
            doc_type,
            source_as_of,
            age=calendar_age,
            unit=policy.unit,
            max_age_days=None,
            policy=policy.past_max_age,
        )

    if policy.unit == "business_days":
        age = business_days_elapsed(source_as_of, today)
    else:
        age = calendar_age
    stale = age > policy.max_age_days  # inclusive boundary — see module docstring

    if not stale:
        verdict = FreshnessVerdict.FRESH
    elif policy.past_max_age is StalePolicy.REFUSE:
        verdict = FreshnessVerdict.REFUSE
    else:
        verdict = FreshnessVerdict.FLAG

    return FreshnessCheck(
        verdict,
        doc_type,
        source_as_of,
        age=age,
        unit=policy.unit,
        max_age_days=policy.max_age_days,
        policy=policy.past_max_age,
    )
