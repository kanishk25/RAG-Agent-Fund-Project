"""Cards → chunks, with the metadata every later stage reads (P3.1, P3.2).

This module is deliberately thin, and that thinness is the design: **one fact
card becomes exactly one chunk.** No splitting, no merging, no sliding window.

That is ARCH §6.1 mitigation 2 — "single-attribute chunks for numeric facts" —
already satisfied upstream. P2.6 renders each extracted fact as its own
one-sentence card, so by the time text reaches here the corpus is already at one
attribute per unit. A chunker that re-split or re-grouped would undo that: merge
two facts and a similarity hit returns a small table the model must parse, which
is precisely the cross-attribute confusion the mitigation exists to prevent.

So why have the module at all, if the transform is 1:1?

  1. **The metadata contract lives here.** `doc_id`, `scheme_id`, `doc_type`,
     `source_url`, `source_as_of`, `chunk_index` (P3.2) plus `card_hash` are
     stamped in one place. `scheme_id` is what the query filter keys on
     (ARCH §6.1 mitigation 1), `source_url` is the citation the P4 validator
     checks an answer against, and `source_as_of` is the footer date. Every one
     of them is load-bearing at query time; none is decorative.
  2. **The 1:1 property is asserted, not assumed.** A test pins it, so a future
     "improvement" that chunks holdings per-line fails loudly rather than
     quietly reintroducing R-08.
  3. **`chunk_id` is defined here** as `{doc_id}#{chunk_index}`, keeping room
     for multi-chunk documents without today's ids having to change.
  4. **The embedded text is derived here** — see below. It is not the served
     text, and the difference is measured rather than assumed.

⚠️ Measured: the scheme name in a card is noise for the embedding
----------------------------------------------------------------
Every card names its scheme (P2.6), which is what makes a card findable *across*
schemes. But retrieval never searches across schemes: the query resolves to one
`scheme_id` and Chroma hard-filters on it (ARCH §6.1 mitigation 1) before
similarity is consulted at all. So by the time vectors are compared, every
candidate shares the same fund name — and for a long name like "Motilal Oswal
BSE Financials ex Bank 30 Index Fund Direct Growth" that shared prefix is most
of a ~32-token card. It dominates the embedding and leaves the seven facts of a
scheme nearly indistinguishable from one another.

Measured over the golden set, ranking within a correctly-filtered scheme:

| Embedded text | recall@1 | recall@4 |
|---|---|---|
| Full card (scheme name included) | 82.4% | 91.2% |
| **Scheme name removed** | **97.1%** | **97.1%** |

"Portfolio holdings of Motilal Oswal Nifty Next 50 Index Fund" ranked the
*benchmark* card first and the holdings card fifth — outside `top_k=4`, so the
right chunk never reached the model. Removing the redundant name fixes it.

So `embed_text` is the card with its scheme's name and aliases removed, and
`text` — what Chroma stores and what P4 sends to the model and cites — remains
the complete card. Only the vector changes; nothing a user or the validator ever
sees does.

**The query side strips too, but differently** (`resolve.strip_scheme_terms`):
cards are generated here, so their exact `display_name` can be removed as a
phrase, cleanly. A user's query cannot — "Motilul Oswal larg and midcap" has to
keep working (Q-09) — so queries strip fuzzily, token by token. The asymmetry is
deliberate: exact where we control the text, tolerant where we do not.

⚠️ Measured: holdings cards exceed the embedding model's input window
--------------------------------------------------------------------
`all-MiniLM-L6-v2` truncates at **256 word-piece tokens**. Measured over the
five fixture schemes: 30 of 35 cards are tiny (median **32** tokens), but all
five holdings cards overflow — `mo_next50:holdings` is **521** tokens, roughly
double the window. Its second half is never seen by the embedding model.

This is accepted rather than fixed, for two reasons that both had to hold:

  - **The discriminative text survives.** A card opens with the scheme name and
    "discloses N portfolio holdings:", so what a query like "top holdings of X"
    actually matches on sits at the front. Truncation eats the tail of a comma-
    separated company list, not the part that makes the card findable. Recall@4
    on the holdings cases is measured, not assumed (see `tests/test_retrieval_eval.py`).
  - **The served text is complete.** Only the *embedding* is truncated. Chroma
    stores the whole card, so the LLM receives every holding — a "top holdings"
    answer is not silently shortened, which was the actual R-08 harm.

**What this does not cover:** "does fund X hold <company>" for a company in the
truncated tail may fail to retrieve. No golden case asks that today. If such
questions are ever added, the fix is to embed a purpose-built retrieval text
while still storing the full card as the document — not to re-split holdings,
which R-08 already ruled out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from mf_faq.ingest.fact_card import FactCard
from mf_faq.ingest.normalise import content_hash
from mf_faq.schemas import SourcesConfig
from mf_faq.settings import get_sources

#: If stripping leaves less than this many characters, the card is embedded
#: whole. Guards a hypothetical card that is nothing but its scheme's name —
#: embedding an empty string would put a meaningless vector in the index.
MIN_EMBED_CHARS = 8


@dataclass(frozen=True)
class Chunk:
    """One embeddable unit plus the metadata the query path depends on."""

    chunk_id: str
    doc_id: str
    scheme_id: str
    doc_type: str
    text: str
    chunk_index: int
    source_url: str
    source_as_of: str
    date_is_page_level: bool
    #: The card with its scheme's name removed — what is actually embedded.
    #: Never served, never cited. See the module docstring for the measurement.
    embed_text: str
    #: Hash of the SERVED text. Carried into the index so a sync can ask "is
    #: what is stored still what we would serve?" without re-embedding to find
    #: out. This is half of what makes `store.sync()` convergent (P3.4).
    card_hash: str

    @property
    def embed_hash(self) -> str:
        """Hash of the EMBEDDED text — the other half of the sync comparison.

        Separate from `card_hash` because the two can move independently:
        adding an alias to `sources.yaml` changes what gets stripped, and so
        changes the vector, while leaving the served card byte-identical. With
        only `card_hash` stored, that edit would leave every vector in the index
        built under the old rule, silently and permanently.
        """
        return content_hash(self.embed_text)

    def metadata(self) -> dict[str, Any]:
        """Chroma metadata. Flat scalars only — Chroma rejects nested values."""
        return {
            "doc_id": self.doc_id,
            "scheme_id": self.scheme_id,
            "doc_type": self.doc_type,
            "source_url": self.source_url,
            "source_as_of": self.source_as_of,
            "chunk_index": self.chunk_index,
            "date_is_page_level": self.date_is_page_level,
            "card_hash": self.card_hash,
            "embed_hash": self.embed_hash,
        }


def chunk_id_for(doc_id: str, chunk_index: int = 0) -> str:
    return f"{doc_id}#{chunk_index}"


def strip_scheme_name(text: str, names: list[str]) -> str:
    """Remove a scheme's display name and aliases from card text.

    Exact, case-insensitive, longest-name-first so "Motilal Oswal ELSS Tax Saver
    Fund" is removed before the alias "elss" can eat a fragment of it. Safe to
    do exactly here because these cards were rendered from these very names.
    """
    out = text
    for name in sorted(names, key=len, reverse=True):
        out = re.sub(re.escape(name), " ", out, flags=re.IGNORECASE)
    out = " ".join(out.split())
    return out if len(out) >= MIN_EMBED_CHARS else text


def chunk_card(card: FactCard, names: list[str]) -> Chunk:
    """One card → one chunk. See the module docstring for why it is not more."""
    return Chunk(
        chunk_id=chunk_id_for(card.doc_id, 0),
        doc_id=card.doc_id,
        scheme_id=card.scheme_id,
        doc_type=card.doc_type,
        text=card.text,
        chunk_index=0,
        source_url=card.source_url,
        source_as_of=card.source_as_of,
        date_is_page_level=card.date_is_page_level,
        embed_text=strip_scheme_name(card.text, names),
        card_hash=card.text_hash,
    )


def chunk_cards(cards: list[FactCard], sources: SourcesConfig | None = None) -> list[Chunk]:
    sources = sources or get_sources()
    names = {s.scheme_id: [s.display_name, *s.aliases] for s in sources.schemes}
    return [chunk_card(c, names[c.scheme_id]) for c in cards]
