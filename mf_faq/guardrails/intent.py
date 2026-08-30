"""Deterministic advisory pre-filter (P4.2, ARCH §7.2).

Two layers exist in this design, cheap first. This module is the first: a
regex-based classifier that catches the phrasings a helpfulness-tuned model
would otherwise be tempted to answer. It costs no tokens, needs no model call,
and cannot be jailbroken by prompt-injection tricks played on an LLM, because
there is no LLM here to play them on. Anything this module misses is still
caught by the second layer — the model's own `is_answerable` judgment, folded
into the same structured-output call as generation (`generation/answer.py`) —
with the validator's advisory-language scan as a final backstop on the output
(ARCH §7.4, G-11).

**This module is not trying to be exhaustive, and that is deliberate.** ARCH
§15.5 names the tradeoff directly: a regex that tries to catch every possible
advisory phrasing turns into a maintenance burden that still misses the next
clever rewording, while a model call reads the actual meaning. So this module
covers the phrasings a lookup table can name safely and specifically — the
ones ARCH's own §7.2 lists ("should I", "is it good", "which is better",
"worth investing", "recommend") plus a modest set found while working the
refusal set — and leaves genuinely evasive framing (e.g. "I want the cheapest
fund — which one has the lowest expense ratio?", Q-04) to the layer built to
read meaning rather than pattern-match words. **Measured against the P0-authored
refusal set: 32 of 33 cases classify deterministically here; one is left to the
model on purpose** (see `tests/test_intent.py::TestTheOneCaseLeftToTheModel`).

Reason taxonomy and link routing
---------------------------------
Every `Reason` this module returns is a key in `config/refusal_links.yaml`'s
`reason_routing` — that file is the single place link choice is decided, and
this module never picks a link itself. Three reasons this module does **not**
produce, because they are not properties of the query text: `pii` (a separate,
earlier gate — ARCH §7.1), `ambiguous_scheme` and `scheme_not_covered` (both
properties of scheme *resolution*, decided by `retrieval.search`, not by
pattern-matching words).

Why some barred content is caught here instead of left to the model
---------------------------------------------------------------------
`fact_not_covered` (riskometer, capital-gains statement, fund manager) and
`plan_not_covered` ("Regular Plan" — the corpus holds Direct plan only, PS §8.4
item 17) are deterministically named, closed sets — there will not be a sixth
excluded fact tomorrow that this regex needs to anticipate, because P0.4 and
P2.2's field allowlist already closed that set for good. Catching them here
costs nothing (no retrieval, no tokens, no RPM) and removes an entire class of
query from the "does the model correctly say no" question, which is exactly
the kind of case ARCH §15.5's decision order asks to reach for first:
strengthen the deterministic gate before tightening the prompt, tighten the
prompt before changing the model.

**Mixed factual + advisory queries are classified, not merely detected, as
mixed** (Q-06). "What is the NAV, and should I buy it?" matches the same
advisory pattern as a pure advisory query, but is labelled `mixed_factual_advisory`
rather than `advisory_direct` when a factual keyword also appears — not because
the routing differs (both map to `educational`), but because refusing here,
before retrieval ever runs, is *how* `must_not_contain_value` (edge case Q-06)
is guaranteed: no chunk is ever fetched, so there is no value in scope to leak
into a refusal message, structurally rather than by care taken at render time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Reason(StrEnum):
    """A subset of `refusal_links.yaml`'s `reason_routing` keys — the ones a
    deterministic read of the query text can name. See the module docstring
    for the three keys deliberately absent from this enum."""

    ADVISORY_DIRECT = "advisory_direct"
    MIXED_FACTUAL_ADVISORY = "mixed_factual_advisory"
    PERFORMANCE_BARRED = "performance_barred"
    INJECTION = "injection"
    BARRED_FIELD = "barred_field"
    FACT_NOT_COVERED = "fact_not_covered"
    PLAN_NOT_COVERED = "plan_not_covered"


@dataclass(frozen=True)
class IntentVerdict:
    """A deterministic refusal, with the pattern name that fired.

    `matched` is diagnostic only — it names which named pattern group fired
    ("advisory", "performance", ...), not the substring of the query that
    matched, so it is safe to log without carrying user text.
    """

    reason: Reason
    matched: str


# Checked first and wins over everything else: an injected instruction is a
# more specific and more urgent classification than the advisory language it
# usually also contains ("ignore instructions and recommend..." matches both
# INJECTION and ADVISORY patterns — injection is reported).
_INJECTION_PATTERNS = {
    "ignore_instructions": re.compile(
        r"\bignore\b.*\b(?:previous|prior|all)\b.*\binstructions?\b", re.IGNORECASE
    ),
    "roleplay_override": re.compile(
        r"\byou are now\b|\bno restrictions?\b|\bact as\b", re.IGNORECASE
    ),
    "system_prompt_probe": re.compile(r"\bsystem prompt\b", re.IGNORECASE),
}

# PS §5.3-barred payload fields (P0.4 / normalise.BARRED_FIELDS) named in a
# question. These exist in Groww's raw payload but were never ingested — a
# question about them cannot be answered by retrieval failing to find a
# chunk, because the ANSWER "we don't track ratings" is itself the refusal.
_BARRED_FIELD_PATTERNS = {
    "groww_rating": re.compile(r"\bgroww rating\b", re.IGNORECASE),
    "pros_and_cons": re.compile(r"\bpros and cons\b", re.IGNORECASE),
    "peer_comparison": re.compile(
        r"\bpeer comparison\b|\bcompare\w* to (?:its |their )?peers?\b", re.IGNORECASE
    ),
}

# In-scope per PS §4.2 in principle, but closed out of THIS corpus by name —
# P0.4 (riskometer, statement/capital-gains process) and the field allowlist
# (fund_manager is in Groww's payload but never in any scheme's `extract:`).
_FACT_NOT_COVERED_PATTERNS = {
    "riskometer": re.compile(r"\brisk[- ]?o?[- ]?meter\b|\brisk rating\b", re.IGNORECASE),
    "statement_process": re.compile(
        r"\bcapital gains? statement\b|\bdownload\b.*\bstatement\b", re.IGNORECASE
    ),
    "fund_manager": re.compile(r"\bfund manager\b|\bwho manages\b", re.IGNORECASE),
}

# PS §8.4 item 17 (Q-17): the corpus holds Direct plan data only.
_PLAN_NOT_COVERED_PATTERNS = {
    "regular_plan": re.compile(r"\bregular\s+(?:plan|growth)\b", re.IGNORECASE),
}

# Return/derived-value language PS §5.3 bars outright. "returns" and "cagr" are
# safe unqualified triggers: no in-scope fact card (nav, expense_ratio,
# exit_load, holdings, min_sip, lock_in, benchmark — see fact_card.py) ever
# uses either word, so there is no legitimate golden-set question to collide
# with.
_PERFORMANCE_PATTERNS = {
    "returns_or_cagr": re.compile(
        r"\breturns?\b|\bcagr\b|\bperformance\b|\boutperform\w*\b", re.IGNORECASE
    ),
    "sip_calculation": re.compile(
        r"\bwhat would (?:it|that) be worth\b|\bworth (?:now|today)\b", re.IGNORECASE
    ),
}


def _matches_beaten_benchmark(text: str) -> bool:
    """'Has X beaten its benchmark?' — a two-word combination, not one phrase.

    Named benchmark IS an in-scope fact (fact_card.py renders it), so a bare
    mention of "benchmark" must not trigger this — only a comparison verb
    ("beat"/"beaten"/"outperform") applied TO the benchmark does.
    """
    lowered = text.casefold()
    return bool(re.search(r"\bbeat(?:en)?\b", lowered)) and "benchmark" in lowered


def _matches_sip_worth_calculation(text: str) -> bool:
    """'If I invested X monthly for Y years, what would it be worth?'

    Two ordinary words ("invested", "worth") that are each fine alone — a
    factual min-SIP question can say "invest" — but together, in a query about
    a hypothetical accumulation, describe a return calculation PS §5.3 bars.
    """
    lowered = text.casefold()
    return "invested" in lowered and "worth" in lowered


# Direct advisory language (ARCH §7.2's own list, plus phrasings found while
# authoring the refusal set). Deliberately not exhaustive — see module
# docstring for why the gap is intentional and where it is caught instead.
_ADVISORY_PATTERNS = {
    "should_i": re.compile(r"\bshould i\b", re.IGNORECASE),
    "is_it_good": re.compile(r"\bis it (?:a |really )?good\b", re.IGNORECASE),
    "good_fund_or_value": re.compile(
        r"\bgood (?:fund|investment|choice)\b|\bgood value\b", re.IGNORECASE
    ),
    "which_is_better": re.compile(r"\bwhich (?:one )?(?:is )?(?:the )?better\b", re.IGNORECASE),
    "worth_investing": re.compile(r"\bworth investing\b|\bworth it\b", re.IGNORECASE),
    "recommend": re.compile(r"\brecommend(?:ed|ation)?\b", re.IGNORECASE),
    "would_you_pick": re.compile(
        r"\b(?:would|will) you (?:pick|choose|recommend|suggest|invest)\b", re.IGNORECASE
    ),
    "suits_me": re.compile(r"\bsuits? me\b", re.IGNORECASE),
    "rank_these": re.compile(r"\brank(?:ed|ing)?\b", re.IGNORECASE),
    "best_or_worst": re.compile(r"\b(?:best|worst) (?:fund|option|choice)\b", re.IGNORECASE),
    "better_option": re.compile(r"\bbetter (?:option|choice|value)\b", re.IGNORECASE),
}

# Factual keywords whose CO-OCCURRENCE with an advisory pattern reclassifies
# the verdict from advisory_direct to mixed_factual_advisory (Q-06). This list
# does not gate anything on its own — it only relabels an advisory match.
_FACTUAL_KEYWORDS = re.compile(
    r"\bnav\b|\bnet asset value\b|\bexpense ratio\b|\bexit load\b|"
    r"\block[- ]?in\b|\b(?:min(?:imum)?\s+)?sip\b|\bbenchmark\b|\bholdings?\b|\bportfolio\b",
    re.IGNORECASE,
)


def _first_match(patterns: dict[str, re.Pattern[str]], text: str) -> str | None:
    for name, pattern in patterns.items():
        if pattern.search(text):
            return name
    return None


def classify(query: str) -> IntentVerdict | None:
    """Classify `query` deterministically, or return None to defer.

    None means: no closed-set pattern fired. It does NOT mean the query is
    answerable — it means the answer depends on which scheme (if any) the
    query resolves to and, ultimately, on the model's own judgment. The caller
    proceeds to scheme resolution and retrieval next.
    """
    if name := _first_match(_INJECTION_PATTERNS, query):
        return IntentVerdict(Reason.INJECTION, name)

    if name := _first_match(_BARRED_FIELD_PATTERNS, query):
        return IntentVerdict(Reason.BARRED_FIELD, name)

    if name := _first_match(_FACT_NOT_COVERED_PATTERNS, query):
        return IntentVerdict(Reason.FACT_NOT_COVERED, name)

    if name := _first_match(_PLAN_NOT_COVERED_PATTERNS, query):
        return IntentVerdict(Reason.PLAN_NOT_COVERED, name)

    if _matches_beaten_benchmark(query):
        return IntentVerdict(Reason.PERFORMANCE_BARRED, "beaten_benchmark")
    if _matches_sip_worth_calculation(query):
        return IntentVerdict(Reason.PERFORMANCE_BARRED, "sip_worth_calculation")
    if name := _first_match(_PERFORMANCE_PATTERNS, query):
        return IntentVerdict(Reason.PERFORMANCE_BARRED, name)

    if name := _first_match(_ADVISORY_PATTERNS, query):
        reason = (
            Reason.MIXED_FACTUAL_ADVISORY
            if _FACTUAL_KEYWORDS.search(query)
            else Reason.ADVISORY_DIRECT
        )
        return IntentVerdict(reason, name)

    return None
