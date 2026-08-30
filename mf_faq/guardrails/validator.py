"""Post-generation validator: five deterministic checks (P4.6, ARCH §7.4).

Every answer the model claims is answerable passes through here before it can
reach a user. **A failure here is never retried with a nudge — it refuses.**
Retrying teaches the model nothing about why it failed and risks a second bad
answer for the same reason; a validator failure and an ordinary refusal look
identical to the caller by design (`ValidationFailure` renders the same way a
`refusal_reason="not_answerable"` response does).

This module is called ONLY for `is_answerable=True` responses — an
`is_answerable=False` response is a refusal already and has no citation to
validate (G-03: "is_answerable wins", checked by the caller before this module
is ever reached).

The five checks, and how G-02 through G-12 map onto them
----------------------------------------------------------

1. **Answer is non-empty** (G-02) — `is_answerable=True` with an empty
   `answer` is not a legitimate positive response; treated as invalid.
2. **Sentence count ≤ 3** (G-01, G-07).
3. **`citation_index` resolves to exactly one retrieved chunk** (G-04). Because
   `generation/prompts.py` has the model cite a *position* in the numbered
   context rather than a URL, "the citation is in the retrieved set" reduces
   to a bounds check — `1 <= citation_index <= len(chunks)` — which is
   stronger than a URL-membership test (see that module's docstring for why a
   URL alone cannot disambiguate several facts from the same scheme page).
4. **Numeric grounding** (G-05, G-08) — every number in `answer` must appear,
   after normalisation, in the cited chunk's own text or its `source_as_of`.
5. **Advisory-language scan** (G-11) — the backstop against a factually correct
   answer phrased as a recommendation, independent of whether `is_answerable`
   should have already caught it.

**"Footer date matches cited chunk" (the fifth check named in the plan) is not
implemented as a sixth runtime comparison — it is guaranteed by construction.**
`generation/render.py` builds the footer from `cited_chunk.source_as_of`
directly; there is no second, independently-produced date for it to disagree
with (G-12: the model's own value is never read for this). What check 3 above
protects is the one way that guarantee could break: an invalid `citation_index`
resolving to the wrong chunk, or no chunk at all. Once check 3 passes, "the
footer date matches the cited chunk" is not a fact that needs checking — it is
not a fact that could be false. `tests/test_validator.py` asserts this
explicitly rather than leaving it as an unstated assumption.

Sentence counting (G-07) and numeric grounding normalisation (G-08)
---------------------------------------------------------------------
Naive `.`-splitting breaks on "Minimum SIP is Rs. 500 per month." (counts 2)
and on "the ratio is 0.62%." (the decimal point looks like a sentence
boundary). `count_sentences` protects a short, closed list of abbreviations
this domain actually produces (`Rs.`, decimals) before splitting — not a
general-purpose sentence tokenizer, which is more machinery than a 5-scheme,
7-doc_type corpus needs.

Grounding compares *normalised* digit runs — commas and currency/percent
symbols stripped, decimal points kept — so `₹500`, `Rs 500`, and `500` ground
identically, and so do `0.62%` and `0.62 %`. `3 years` vs `36 months` needs no
special equivalence table: `fact_card.py`'s `_lock_in_phrase` already renders
**both** forms into the card text for exactly this reason (G-08), so both
digit tokens are separately, literally present.

**G-06 is a known, accepted gap, not something this module claims to catch.**
A chunk that exists for a *different* attribute of the same scheme can still
ground a wrong-attribute answer numerically — a quoted TER value is a real
number that really appears in a real chunk, just the wrong chunk's fact. Edge
cases.md records this as a real limit ("only the golden set catches this");
this validator is not pretending otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mf_faq.generation.answer import FactAnswer
from mf_faq.retrieval.store import RetrievedChunk

MAX_SENTENCES = 3

# Domain abbreviations that must not be treated as sentence boundaries. Kept
# short and specific to what this corpus's fact cards and answers actually
# produce — not an attempt at general-purpose abbreviation handling.
_ABBREVIATIONS = (
    "Rs.",
    "No.",
    "w.e.f.",
    "Ltd.",
    "Dr.",
    "Mr.",
    "Mrs.",
    "vs.",
    "e.g.",
    "i.e.",
    "etc.",
)
_SENTINEL = "\x00"

_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?]+(?:\s+|$)")
_DECIMAL_RE = re.compile(r"(?<=\d)\.(?=\d)")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# G-11 — phrasing that recommends or judges, independent of is_answerable.
_ADVISORY_LANGUAGE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\byou should\b",
        r"\bwe recommend\b",
        r"\bit(?:'s| is) recommended\b",
        r"\bbetter option\b",
        r"\ba good (?:choice|fund|investment)\b",
        r"\battractive\b",
        r"\bideal for\b",
        r"\bworth investing\b",
        r"\badvisable\b",
        r"\bconsider investing\b",
    )
]


class Check:
    """Names of the checks a `ValidationFailure` can report — for logging."""

    ANSWER_NOT_EMPTY = "answer_not_empty"
    SENTENCE_COUNT = "sentence_count"
    CITATION_RANGE = "citation_range"
    NUMERIC_GROUNDING = "numeric_grounding"
    ADVISORY_LANGUAGE = "advisory_language"


@dataclass(frozen=True)
class ValidationFailure:
    """Which check failed, and a detail safe to log.

    `detail` may quote the model's own answer text — that is model output, not
    user-supplied text, so it carries none of the PII-logging restriction that
    applies to the raw query (P-08). It is never the user's query.
    """

    check: str
    detail: str


@dataclass(frozen=True)
class ValidatedAnswer:
    """An answer that passed every check, with its cited chunk resolved.

    `cited_chunk` is what `render.py` builds the citation and footer from —
    never anything the model said about its own citation.
    """

    text: str
    cited_chunk: RetrievedChunk


def count_sentences(text: str) -> int:
    """Sentence count that survives 'Rs.', decimals, and common abbreviations.

    Not a general sentence tokenizer — a closed, small set of protections
    against exactly the shapes this domain's answers produce (G-07).
    """
    protected = _DECIMAL_RE.sub(_SENTINEL, text)
    for abbr in _ABBREVIATIONS:
        protected = protected.replace(abbr, abbr.replace(".", _SENTINEL))
    pieces = [p for p in _SENTENCE_BOUNDARY_RE.split(protected) if p.strip()]
    return len(pieces)


def _extract_numbers(text: str) -> set[str]:
    """Digit runs, normalised: commas stripped, decimal point kept.

    Currency symbols (₹) and percent signs are never captured by the pattern
    in the first place, so `₹500` and `500%` both contribute the bare number —
    exactly the equivalence G-08 asks for, with no separate symbol-stripping
    step needed.
    """
    return {m.group().replace(",", "") for m in _NUMBER_RE.finditer(text)}


def _scan_advisory_language(text: str) -> str | None:
    for pattern in _ADVISORY_LANGUAGE_PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return None


def validate(
    answer: FactAnswer, chunks: list[RetrievedChunk]
) -> ValidatedAnswer | ValidationFailure:
    """Validate a `is_answerable=True` response. Never call this for a refusal.

    `chunks` is the SAME numbered list shown to the model (`PromptChunk` order
    preserved as plain `RetrievedChunk`s) — `citation_index` is 1-based into
    exactly this list.
    """
    if not answer.is_answerable:
        raise ValueError("validate() is for is_answerable=True responses only")

    if not answer.answer.strip():
        return ValidationFailure(Check.ANSWER_NOT_EMPTY, "is_answerable=true but answer is empty")

    sentences = count_sentences(answer.answer)
    if sentences > MAX_SENTENCES:
        return ValidationFailure(
            Check.SENTENCE_COUNT, f"{sentences} sentences (max {MAX_SENTENCES})"
        )

    index = answer.citation_index
    if index is None or not (1 <= index <= len(chunks)):
        return ValidationFailure(
            Check.CITATION_RANGE,
            f"citation_index={index!r} out of range for {len(chunks)} retrieved chunk(s)",
        )
    cited_chunk = chunks[index - 1]

    groundable = _extract_numbers(cited_chunk.text) | _extract_numbers(cited_chunk.source_as_of)
    claimed = _extract_numbers(answer.answer)
    ungrounded = claimed - groundable
    if ungrounded:
        return ValidationFailure(
            Check.NUMERIC_GROUNDING,
            f"numbers {sorted(ungrounded)} do not appear in the cited chunk",
        )

    if pattern := _scan_advisory_language(answer.answer):
        return ValidationFailure(Check.ADVISORY_LANGUAGE, f"matched pattern {pattern!r}")

    return ValidatedAnswer(text=answer.answer, cited_chunk=cited_chunk)
