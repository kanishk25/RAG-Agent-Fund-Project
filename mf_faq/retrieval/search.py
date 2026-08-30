"""Query → scored chunks, or a named reason there are none (P3.6, P3.7).

The whole module exists to make one thing true: **an empty result is never just
an empty list.** Every path that returns no chunks returns *why*, because P4
must say different things in each case and the difference is not cosmetic.

| Outcome | Situation | What P4 must say |
|---|---|---|
| `OK` | chunks above the floor | answer from them |
| `SCHEME_AMBIGUOUS` | query fits 2+ schemes (Q-08) | list them, **never pick** |
| `SCHEME_UNRESOLVED` | no fund named, or one not held (Q-07/Q-10) | ask which fund |
| `EMPTY_INDEX` | nothing ingested yet (R-03) | service unavailable — ops fault, not the user's |
| `NO_FACTS_FOR_SCHEME` | held scheme, no chunks (R-04) | "not available", **not** "unknown fund" |
| `BELOW_FLOOR` | hits exist, all too weak (R-01) | refuse; weak chunks never passed on |

Collapsing any two of these produces a confidently wrong message. R-04 is the
sharp one: a scheme whose every fact was date-rejected at ingest looks identical
to an unknown scheme if you only count chunks, and telling a user we do not
cover a fund we demonstrably do cover is a worse answer than admitting the fact
is missing.

The similarity floor
--------------------
`similarity = 1 - cosine_distance`, from a cosine-space collection over
normalised vectors (see `store.py`). Below `settings.similarity_floor`
(default 0.35, tuned in P5) a chunk is **dropped, not down-weighted**: R-01
requires that no weak chunk reach the model, because a retrieved chunk is an
invitation to answer from it and the model will accept.

**R-02 — the boundary is inclusive.** A score exactly equal to the floor
passes. Stated here because "≥ or >" is precisely the sort of detail that gets
decided twice, differently, and the P5 tuning runs need it fixed. The boundary
is pinned by a test rather than by this sentence.

**R-07 is structurally impossible, not handled.** Two chunks with conflicting
values for one fact cannot arise: `documents` is unique on
`(scheme_id, doc_type)` and `chunk_id` is derived from `doc_id`, so a second
value for a fact overwrites the first rather than joining it. The tie-break
rule the edge case asks for would be unreachable code, and unreachable code that
looks like a safety net is worse than none.

**No `doc_type` filter is applied**, and this was decided by measurement rather
than by preference. ARCH §6.3 permits one "when the query maps to one", and the
first measured run made a case for it: recall@4 was **91.2%**, with holdings
questions ranking the holdings card fifth of seven.

But the cause turned out not to be a missing filter. Every card led with its
scheme's name, which — after the scheme filter has already run — is text every
candidate shares, so it drowned out the part that distinguishes them. Removing
it (`chunk.py`, `resolve.strip_scheme_terms`) lifted recall@4 to **97.1%** and
recall@1 from 82.4% to **97.1%**, which is the stronger result a `doc_type`
filter was being considered for.

A filter is therefore still not applied. Its failure mode — silently excluding
the right chunk because the query's wording mapped to the wrong `doc_type` — is
the one thing retrieval must never do, and it would now be paying that risk to
recover ground that has already been recovered. Reach for it only if a later
measurement, not an intuition, calls for it.

The single remaining golden miss is `G-multi-elss-identify` ("which of these is
an ELSS fund?", tagged `doc_type: lock_in`). Its answer comes from the scheme's
identity rather than from any one fact, so no ranking of that scheme's facts
puts `lock_in` on top. It is recorded as a known miss rather than tuned away —
the eval sets were authored before this code precisely so they would not be
rewritten to fit it (P0 sequencing note 2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mf_faq.logging_setup import get_logger
from mf_faq.retrieval.resolve import Resolution, SchemeMatch, SchemeResolver, get_resolver
from mf_faq.retrieval.store import RetrievedChunk, VectorStore
from mf_faq.settings import get_settings

log = get_logger(__name__)


class SearchOutcome(StrEnum):
    OK = "ok"
    SCHEME_AMBIGUOUS = "scheme_ambiguous"
    SCHEME_UNRESOLVED = "scheme_unresolved"
    EMPTY_INDEX = "empty_index"
    NO_FACTS_FOR_SCHEME = "no_facts_for_scheme"
    BELOW_FLOOR = "below_floor"


@dataclass(frozen=True)
class SearchResult:
    """Chunks, or a named reason there are none."""

    outcome: SearchOutcome
    chunks: list[RetrievedChunk] = field(default_factory=list)
    scheme: SchemeMatch | None = None
    #: Hits that existed but fell below the floor. Never passed to the model —
    #: kept so tuning can see how near a miss was (P5.5).
    discarded: list[RetrievedChunk] = field(default_factory=list)
    floor: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome is SearchOutcome.OK

    @property
    def scheme_id(self) -> str | None:
        return self.scheme.scheme_id if self.scheme else None

    @property
    def candidates(self) -> tuple[str, ...]:
        return self.scheme.candidates if self.scheme else ()

    @property
    def best_similarity(self) -> float | None:
        scored = self.chunks or self.discarded
        return max((c.similarity for c in scored), default=None)


class Searcher:
    """Binds a store and a resolver. Built once; `search` is stateless."""

    def __init__(
        self,
        store: VectorStore,
        *,
        resolver: SchemeResolver | None = None,
        top_k: int | None = None,
        similarity_floor: float | None = None,
    ):
        settings = get_settings()
        self.store = store
        self.resolver = resolver or get_resolver()
        self.top_k = top_k if top_k is not None else settings.top_k
        self.similarity_floor = (
            similarity_floor if similarity_floor is not None else settings.similarity_floor
        )

    def search(self, query: str) -> SearchResult:
        """Resolve, filter, score, and enforce the floor."""
        # R-03 first: with nothing ingested, every other outcome would be a
        # misdiagnosis of what is really an operational fault.
        if self.store.count() == 0:
            log.warning("search against an empty index")
            return SearchResult(outcome=SearchOutcome.EMPTY_INDEX, floor=self.similarity_floor)

        match = self.resolver.resolve(query)
        if match.outcome is Resolution.AMBIGUOUS:
            return self._nothing(SearchOutcome.SCHEME_AMBIGUOUS, match)
        if match.outcome is Resolution.NO_SCHEME:
            return self._nothing(SearchOutcome.SCHEME_UNRESOLVED, match)

        # The scheme is already pinned by the filter below, so its name in the
        # query only crowds the vector — measured in `chunk.py`, mirrored here.
        embedding = self.store.embedder.encode_query(
            self.resolver.strip_scheme_terms(query, match.scheme_id)
        )
        hits = self.store.query(embedding, scheme_id=match.scheme_id, top_k=self.top_k)

        if not hits:
            # The scheme resolved and the index is populated, so this is R-04:
            # we hold the fund but hold no facts for it.
            return self._nothing(SearchOutcome.NO_FACTS_FOR_SCHEME, match)

        # R-02: inclusive. A chunk exactly at the floor is kept.
        kept = [c for c in hits if c.similarity >= self.similarity_floor]
        dropped = [c for c in hits if c.similarity < self.similarity_floor]

        outcome = SearchOutcome.OK if kept else SearchOutcome.BELOW_FLOOR
        log.info(
            "search complete",
            extra={
                "outcome": outcome.value,
                "scheme_id": match.scheme_id,
                "kept": len(kept),
                "dropped": len(dropped),
                "best": round(max(c.similarity for c in hits), 4),
                "floor": self.similarity_floor,
            },
        )
        return SearchResult(
            outcome=outcome,
            chunks=kept,
            scheme=match,
            discarded=dropped,
            floor=self.similarity_floor,
        )

    def _nothing(self, outcome: SearchOutcome, match: SchemeMatch) -> SearchResult:
        log.info(
            "search returned no chunks",
            extra={
                "outcome": outcome.value,
                "reason": match.reason,
                "candidates": list(match.candidates),
            },
        )
        return SearchResult(outcome=outcome, scheme=match, floor=self.similarity_floor)


def build_searcher(**kwargs) -> Searcher:
    """Open the configured store and wrap it. Loads the embedding model lazily."""
    return Searcher(VectorStore.open(), **kwargs)
