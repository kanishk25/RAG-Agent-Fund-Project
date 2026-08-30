"""Render each extracted fact as a natural-language card (P2.6, ARCH §6.2).

A card is the text that gets embedded and hashed. Scraped values retrieve badly
as bare numbers in stray markup, so each fact becomes a real sentence naming its
scheme — which gives the embedding model something to match on and delivers the
single-attribute chunks ARCH §6.1 depends on.

**The date is never in card text. It lives in metadata only.**

PS §9 requires this for the three page-level-dated facts (min_sip, lock_in,
benchmark), whose `nav_date` advances daily regardless of value. Measured in
P2.6, the constraint is broader than that: `historic_fund_expense` has
`frequency: Daily`, so `expense_ratio`'s `as_on_date` also advances every day —
the ELSS ratio sat at 0.97 for 48 consecutive days while its date moved each
one. A date in the card would change the text daily, change the hash daily, and
re-embed a fact that had not moved.

So one uniform rule: **card text carries the value; `source_as_of` rides in
metadata.** PS §8.7's requirement that an answer state the NAV declaration date
is met at render time (P4.9) from that metadata, via `render_context()` — the
LLM sees the date, the embedding never does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mf_faq.ingest.normalise import content_hash
from mf_faq.ingest.parse import ExtractedFact
from mf_faq.schemas import SchemeConfig

# Holdings are one card, not one per holding: with 32-52 holdings and top_k=4,
# per-holding chunks would truncate a "top holdings" answer into a confident but
# incomplete list (edge case R-08).
MAX_HOLDINGS_IN_CARD = 60


@dataclass(frozen=True)
class FactCard:
    """One embeddable chunk: stable text plus retrieval/citation metadata.

    **Two hashes, because change detection asks two different questions (P2.7).**

    `value_hash` hashes the parsed value and answers *did the fact move?* — a
    compliance question, since a value that moves without its date moving would
    be served under a stale footer (edge case I-09).

    `text_hash` hashes the embedded sentence and answers *must we re-embed?* — a
    cost question.

    They come apart in both directions. A card-renderer revision changes the
    text while the fact stands still; a value whose rendering rounds identically
    would change the value while the text stands still. Storing one hash and
    inferring the other would make a renderer edit look like corpus-wide
    upstream tampering.
    """

    doc_id: str
    scheme_id: str
    doc_type: str
    text: str
    value_hash: str
    source_url: str
    source_as_of: str  # ISO date — metadata ONLY, never inside `text`
    date_is_page_level: bool

    @property
    def text_hash(self) -> str:
        """Hash of the embedded text — the re-embed decision (P2.7).

        Deliberately hashes the *card*, not the raw value: the invariant that
        matters for cost is "re-embed iff the embedded text changed". If card
        rendering is ever revised, re-embedding is correct.
        """
        return content_hash(self.text)

    def metadata(self) -> dict[str, Any]:
        """Chroma chunk metadata (ARCH §9). Every field is used at query time."""
        return {
            "doc_id": self.doc_id,
            "scheme_id": self.scheme_id,
            "doc_type": self.doc_type,
            "source_url": self.source_url,
            "source_as_of": self.source_as_of,
            "date_is_page_level": self.date_is_page_level,
        }


def _money(amount: float | int) -> str:
    """Format rupees without inventing precision."""
    if isinstance(amount, int) or float(amount).is_integer():
        return f"₹{int(amount):,}"
    return f"₹{amount}"


def _lock_in_phrase(value: dict) -> str:
    if not value.get("has_lock_in"):
        return "has no lock-in period"
    years, months, days = value.get("years", 0), value.get("months", 0), value.get("days", 0)
    parts = []
    if years:
        # Both forms appear on purpose: the validator's numeric-grounding check
        # requires the answer's numbers to appear verbatim in chunk text, and a
        # correct answer may say either "3 years" or "36 months" (edge case G-08).
        parts.append(f"{years} year{'s' if years != 1 else ''} ({years * 12} months)")
    if months:
        parts.append(f"{months} month{'s' if months != 1 else ''}")
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    return "has a lock-in period of " + ", ".join(parts)


def _holdings_phrase(value: dict) -> str:
    """Verbatim disclosure only — names and weights, no commentary (PS §8.2)."""
    entries = value["holdings"][:MAX_HOLDINGS_IN_CARD]
    listed = ", ".join(f"{e['company_name']} {e['corpus_per']}%" for e in entries)
    return f"discloses {value['count']} portfolio holdings: {listed}."


def render_card(fact: ExtractedFact, scheme: SchemeConfig) -> FactCard:
    """Turn one extracted fact into an embeddable card."""
    name = scheme.display_name
    value = fact.value

    # The scheme name appears exactly ONCE per card. It has to appear — it is
    # what a semantic query matches on — but repeating it as a leading label
    # plus again in the sentence wastes tokens on every retrieval, and retrieved
    # chunks are re-sent against the 8K TPM ceiling on each request (ARCH §15.4).
    if fact.doc_type == "nav":
        text = f"The latest declared NAV (net asset value) of {name} is ₹{value} per unit."
    elif fact.doc_type == "expense_ratio":
        text = f"The total expense ratio (TER) of {name} is {value}% per annum."
    elif fact.doc_type == "exit_load":
        text = f"The exit load of {name} is: {value}"
        if not text.endswith("."):
            text += "."
    elif fact.doc_type == "min_sip":
        text = f"The minimum SIP (systematic investment plan) amount for {name} is {_money(value)}."
    elif fact.doc_type == "lock_in":
        text = f"{name} {_lock_in_phrase(value)}."
    elif fact.doc_type == "benchmark":
        text = f"The benchmark index of {name} is {value}."
    elif fact.doc_type == "holdings":
        text = f"{name} {_holdings_phrase(value)}"
    else:  # pragma: no cover - EXTRACTORS and this dispatch are tested in lockstep
        raise ValueError(f"no card renderer for doc_type '{fact.doc_type}'")

    return FactCard(
        doc_id=fact.doc_id,
        scheme_id=fact.scheme_id,
        doc_type=fact.doc_type,
        text=" ".join(text.split()),  # collapse incidental whitespace
        value_hash=content_hash(fact.value),
        source_url=fact.source_url,
        source_as_of=fact.source_as_of.isoformat(),
        date_is_page_level=fact.date_is_page_level,
    )


def render_cards(facts: list[ExtractedFact], scheme: SchemeConfig) -> list[FactCard]:
    return [render_card(f, scheme) for f in facts]


def render_context(cards: list[FactCard]) -> str:
    """Assemble retrieved cards into LLM context (used in P4).

    This is where `source_as_of` re-enters — as a labelled metadata line beside
    the card, never inside the embedded text. That split is what lets an answer
    state the NAV declaration date (PS §8.7) while the embedding stays stable
    across days when the value has not moved.
    """
    blocks = []
    for i, card in enumerate(cards, start=1):
        blocks.append(
            f"[{i}] {card.text}\n"
            f"    source_url: {card.source_url}\n"
            f"    source_as_of: {card.source_as_of}"
        )
    return "\n\n".join(blocks)
