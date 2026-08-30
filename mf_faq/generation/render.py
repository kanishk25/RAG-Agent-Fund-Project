"""Assembles the final response: answer + citation + footer, or a refusal
(P4.7, P4.9).

**The footer is built from `ValidatedAnswer.cited_chunk`, never from anything
the model said** (G-12). That chunk is what `guardrails/validator.py`
resolved from a bounds-checked `citation_index` — by the time this module
runs, "which fact is this answer about" has already been pinned to exactly
one retrieved chunk, so there is only one date and one URL this function could
possibly render (see `validator.py`'s docstring on why "footer matches cited
chunk" needs no separate runtime check).

**Every refusal path returns a working link or explicitly none** — never a
missing field. `config/refusal_links.yaml`'s `reason_routing` is the single
place that decision lives; this module never picks a link itself, it only
asks `link_for(reason, scheme_id)` and renders whatever comes back (including
`None` for `pii` and `ambiguous_scheme`, where PS §5.2 and plain politeness
both argue against sending a user anywhere while their query is unresolved).

**A refusal never carries chunk data.** Every renderer here that produces a
refusal takes, at most, a `SchemeConfig` (a name to phrase the message
around) — never a `RetrievedChunk` or a fact value. This is what makes Q-06's
`must_not_contain_value` a structural guarantee rather than a wording
convention: there is no code path from "we decided to refuse" to "here is a
value" for these functions to accidentally take.
"""

from __future__ import annotations

from pydantic import BaseModel

from mf_faq.guardrails.intent import Reason
from mf_faq.guardrails.validator import ValidatedAnswer, ValidationFailure
from mf_faq.schemas import LinkTarget, SchemeConfig
from mf_faq.settings import get_refusal_links

DISCLAIMER = "Facts-only. No investment advice."


class AskResponse(BaseModel):
    """The full `/ask` response shape (P4.8's contract)."""

    answered: bool
    text: str
    citation_url: str | None = None
    source_as_of: str | None = None
    stale: bool = False
    refusal_reason: str | None = None
    link: LinkTarget | None = None
    disclaimer: str = DISCLAIMER


def render_answer(validated: ValidatedAnswer, *, stale: bool = False) -> AskResponse:
    """A validated, citable answer. `stale` renders the P7.4 flag for a
    FLAG-verdict (not refused) freshness result — the fact is answerable but
    past its normal freshness window."""
    chunk = validated.cited_chunk
    text = validated.text
    if stale:
        text += " (Note: this figure may be older than usual — please verify against the source.)"
    return AskResponse(
        answered=True,
        text=text,
        citation_url=chunk.source_url,
        source_as_of=chunk.source_as_of,
        stale=stale,
    )


def _refusal(reason: str, text: str, *, scheme_id: str | None = None) -> AskResponse:
    link = get_refusal_links().link_for(reason, scheme_id)
    return AskResponse(answered=False, text=text, refusal_reason=reason, link=link)


def render_pii_refusal() -> AskResponse:
    """P4.1 — no link, no chunk, nothing beyond a privacy notice (P-01..P-08)."""
    text = (
        "I can't process a message that appears to contain personal "
        "information (such as a PAN, phone number, Aadhaar, or account "
        "number). Please ask your question again without including it."
    )
    return AskResponse(answered=False, text=text, refusal_reason="pii", link=None)


def render_ambiguous_refusal(candidates: list[SchemeConfig]) -> AskResponse:
    """Q-08 — lists every candidate, picks none."""
    names = "; ".join(s.display_name for s in candidates)
    text = (
        f"That could refer to more than one fund I cover: {names}. "
        "Could you say which one you mean?"
    )
    return _refusal("ambiguous_scheme", text)


def render_unresolved_scheme_refusal(all_schemes: list[SchemeConfig]) -> AskResponse:
    """Q-07 (no fund named) and Q-10 (a fund I don't cover) — see the P3
    finding on why these two intentionally share one rendering."""
    names = "; ".join(s.display_name for s in all_schemes)
    text = (
        f"I can only answer questions about these 5 Motilal Oswal schemes: {names}. "
        "Could you name one of these, or rephrase your question?"
    )
    return _refusal("scheme_not_covered", text)


def render_no_facts_refusal(scheme: SchemeConfig) -> AskResponse:
    """R-04 — the scheme resolved; the specific fact is not on record for it."""
    text = f"I don't have that specific fact on record for {scheme.display_name}."
    return _refusal("fact_not_covered", text, scheme_id=scheme.scheme_id)


def render_below_floor_refusal(scheme: SchemeConfig | None = None) -> AskResponse:
    """R-01 — retrieval found nothing confident enough to answer from."""
    text = "I don't have information that confidently answers that question."
    return _refusal("fact_not_covered", text, scheme_id=scheme.scheme_id if scheme else None)


_INTENT_REFUSAL_TEXT: dict[Reason, str] = {
    Reason.ADVISORY_DIRECT: "I can only provide factual information — I can't offer "
    "investment advice or recommendations.",
    Reason.MIXED_FACTUAL_ADVISORY: "I can't answer that question as asked, because it "
    "also asks for advice — I only provide plain facts.",
    Reason.PERFORMANCE_BARRED: "I don't provide historical returns, CAGR, or performance "
    "calculations. Please check the official factsheet for that.",
    Reason.INJECTION: "I can only answer factual questions about the mutual fund schemes I cover.",
    Reason.BARRED_FIELD: "That isn't information I track for these schemes.",
    Reason.FACT_NOT_COVERED: "That fact isn't available for this scheme from my sources.",
    Reason.PLAN_NOT_COVERED: "I only hold data for the Direct plan of these schemes, not "
    "the Regular plan.",
}


def render_intent_refusal(reason: Reason, *, scheme_id: str | None = None) -> AskResponse:
    """The deterministic pre-filter's refusal (P4.2)."""
    text = _INTENT_REFUSAL_TEXT[reason]
    return _refusal(reason.value, text, scheme_id=scheme_id)


def render_model_refusal() -> AskResponse:
    """The model's own is_answerable=false judgment (a disguised advisory
    phrasing, or a right-scheme-wrong-fact question — R-05). The model's
    stated `refusal_reason` is logged elsewhere for debugging; it never
    drives what is said here or which link is shown (see prompts.py)."""
    text = (
        "I can only answer with plain facts on record for these funds, "
        "and I can't answer that as asked."
    )
    return _refusal("not_answerable", text)


def render_validation_failure_refusal(failure: ValidationFailure) -> AskResponse:
    """A response the model produced but that failed a post-generation check
    (ARCH §7.4). To the user this reads exactly like any other refusal — the
    specific `failure.check` is for logs, never for the message text."""
    text = "I wasn't able to produce a reliably sourced answer to that question."
    return _refusal("not_answerable", text)


def render_stale_refusal(scheme: SchemeConfig, doc_type: str) -> AskResponse:
    """The freshness gate's REFUSE verdict (P4.3) — currently only `nav`."""
    text = (
        f"The {doc_type} I have on record for {scheme.display_name} is too old to state "
        "as current. Please check the official factsheet for the latest figure."
    )
    return _refusal("stale_content", text, scheme_id=scheme.scheme_id)


def render_empty_query() -> AskResponse:
    """Q-12 — empty or whitespace-only. A validation error, not a compliance
    refusal or an outage; no LLM call and no link."""
    text = "Please ask a question about one of the mutual fund schemes I cover."
    return AskResponse(answered=False, text=text, refusal_reason="empty_query", link=None)


def render_query_too_long(max_chars: int) -> AskResponse:
    """Q-13 — rejected before spending a token on it (ARCH §15.4)."""
    text = f"That question is too long (over {max_chars} chars). Please shorten it."
    return AskResponse(answered=False, text=text, refusal_reason="query_too_long", link=None)


def render_service_unavailable() -> AskResponse:
    """R-03 (empty index) and `generation.answer.Unavailable` (429 exhausted,
    a malformed model response, or any other API failure). Deliberately no
    link and no `reason_routing` lookup — this is an operational fault, not a
    compliance decision, and must read that way rather than as a refusal."""
    text = "The assistant is temporarily unavailable. Please try again in a moment."
    return AskResponse(answered=False, text=text, refusal_reason="service_unavailable", link=None)
