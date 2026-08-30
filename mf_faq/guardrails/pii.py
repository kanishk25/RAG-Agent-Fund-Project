"""PII gate — the first thing every query passes through (P4.1, ARCH §7.1).

Runs before logging, before scheme resolution, before retrieval, before any
LLM call. PS §5.2 forbids *processing* PII, and a log line, a retrieval
embedding, and a model call are all processing — so this gate has to be the
very first thing `generation/pipeline.py` does with the raw query text, not
merely the first *guardrail*.

**The decision is binary and the query is never partially handled.** A query
with PII embedded in an otherwise-valid question ("my PAN is ABCDE1234F, what's
the NAV?") is rejected whole (P-07) — stripping the PII substring and answering
the rest would still mean the PII was processed to find it, and it teaches
nothing about whether more is hiding in the same message.

**What a caller may log:** the `PIIKind` alone, never the matched text and
never the query. `PIIFinding` deliberately carries no matched-substring field —
there is no attribute to accidentally log, which is a stronger guarantee than a
comment telling call sites not to (P-08).

The threshold for P-06
-----------------------
The tension this module exists to resolve: block real PII (phone, Aadhaar,
account numbers) without blocking the numbers this system's own answers are
built from — a 6-digit AMFI scheme code, a NAV like `31.4782`, a minimum SIP of
`500`. All of those are short or contain a decimal point; real PII digit-runs
(phone: 10, Aadhaar: 12, most account numbers: 10+) are not. So the rule is a
**tight length floor on a run of consecutive digits**, not a loose "contains
digits" check: `MIN_DIGIT_RUN = 10`. A 6-digit scheme code never reaches it. A
NAV's decimal point breaks the digit run before it can either — `31.4782`
normalises to two runs, `31` and `4782`, both far under the floor. Rupee
amounts with comma grouping (`1,00,000`) are deliberately **not** stripped of
their commas for the same reason: doing so would merge them into one long run
and block a legitimate figure a user typed in good faith. Only whitespace and
hyphens are collapsed before the digit-run check runs, because those are the
separators actually used to break up a phone or Aadhaar number for readability
(P-02, P-03) — a comma is not that; it is how India groups large numbers.

The corollary of a tight floor is more over-blocking at the boundary (P-05: a
12-digit folio-like number that is not actually PII still blocks). That is the
accepted trade — the alternative, a looser rule to admit fewer false positives,
would risk under-blocking a real phone or account number, and leaking PII is
categorically worse than refusing an odd non-PII query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class PIIKind(StrEnum):
    """What kind of PII was found. This — and only this — is safe to log."""

    PAN = "pan"
    AADHAAR = "aadhaar"
    PHONE = "phone"
    ACCOUNT_NUMBER = "account_number"
    EMAIL = "email"


@dataclass(frozen=True)
class PIIFinding:
    """A detection result. Deliberately has no field for the matched text.

    Every other guardrail in this codebase carries evidence (matched tokens,
    reasons, chunk ids) so a decision can be explained after the fact. This one
    does not, on purpose: the evidence *is* the PII, and P-08 requires that logs
    never carry it. An attribute that does not exist cannot be logged by
    accident.
    """

    kind: PIIKind


# PAN: 5 letters, 4 digits, 1 letter — e.g. ABCDE1234F. Case-insensitive since a
# user may not type it in the canonical uppercase form the physical card uses.
_PAN_RE = re.compile(r"\b[A-Za-z]{5}[0-9]{4}[A-Za-z]\b")

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Collapses the separators actually used to break up a phone or Aadhaar number
# for readability (P-02: "1234 5678 9012", P-03: "98765-43210"). Commas are
# deliberately excluded — see the module docstring on Indian rupee grouping.
_SEPARATOR_RE = re.compile(r"[\s-]+")

# The P-06 threshold: a 6-digit AMFI scheme code must not trigger; a phone (10),
# Aadhaar (12), or typical account number (10+) must.
MIN_DIGIT_RUN = 10
_DIGIT_RUN_RE = re.compile(rf"\d{{{MIN_DIGIT_RUN},}}")


def detect_pii(text: str) -> PIIFinding | None:
    """Check `text` for PII. Returns the first kind found, or None.

    Order matters only in that PAN and email are checked against the raw text
    (their patterns are self-delimiting and need no separator collapsing),
    while the digit-run check runs against a whitespace/hyphen-collapsed copy.
    Checking more than one kind would not change the caller's behaviour — any
    finding rejects the whole query — so the first hit is returned rather than
    an exhaustive list.
    """
    if _PAN_RE.search(text):
        return PIIFinding(PIIKind.PAN)
    if _EMAIL_RE.search(text):
        return PIIFinding(PIIKind.EMAIL)

    collapsed = _SEPARATOR_RE.sub("", text)
    match = _DIGIT_RUN_RE.search(collapsed)
    if match:
        length = len(match.group())
        if length == 10:
            return PIIFinding(PIIKind.PHONE)
        if length == 12:
            return PIIFinding(PIIKind.AADHAAR)
        return PIIFinding(PIIKind.ACCOUNT_NUMBER)

    return None


def contains_pii(text: str) -> bool:
    return detect_pii(text) is not None
