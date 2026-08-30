"""Chroma collection: writes, reconciliation, and filtered reads (P3.4).

**The collection is cosine-space.** Chroma defaults to L2, on which the 0.35
similarity floor would be meaningless — an unbounded, scale-dependent number.
`embedder.py` normalises every vector and this module sets
`hnsw:space: cosine`, so Chroma returns `1 - cos` and `similarity(d) = 1 - d`
is a genuine cosine in [-1, 1]. Both halves are required; either alone silently
mis-scales the floor for every query the system ever answers.

Why `sync()` compares the index instead of trusting the run's decisions
-----------------------------------------------------------------------
The obvious wiring is to embed exactly `RunReport.cards_to_embed()` — the facts
P2.7 decided were NEW or CHANGED. It is wrong, and it fails in the way that is
hardest to notice.

The registry and the index are two artifacts written in two steps. Anything that
interrupts the second — a crash after the SQLite commit, an `data/chroma/`
deleted by hand, a bad merge, a machine where embedding failed — leaves the
registry describing facts the index does not hold. On the next run the registry
compares equal, every decision comes back `UNCHANGED`, `cards_to_embed()` is
empty, and **the gap is never repaired.** The index stays short a fact, and the
only symptom is a question that quietly retrieves nothing.

So `sync()` derives the work from what the index actually contains: each chunk
carries its `card_hash` in metadata, and anything missing or divergent is
re-embedded whether or not this run's decisions asked for it. Re-running
converges on a correct index from *any* starting state, including an empty
directory. Repairs are counted separately (`SyncReport.repaired`) precisely
because a non-zero count means drift happened and is worth knowing about.

Two things `sync()` must not touch
----------------------------------
**Quarantined facts (I-09).** When a value moves under a frozen date, P2.7
rejects the update and the registry keeps the *previous* value. The index must
keep the matching previous text — so conflicted `doc_id`s are neither written
nor deleted. Writing the new text would serve exactly the value-under-stale-date
pairing the quarantine exists to prevent.

**Anything outside the run's scope.** `--scheme mo_elss` produces cards for one
scheme; deleting every id that is "not in the desired set" would erase the other
four. Deletion is confined to the scopes actually crawled, and within those it
is driven by the registry: an index entry is removed only when `documents` has
no row for it. That keeps the index a mirror of the registry rather than a
mirror of the last run's cards — which matters for a fact the page stopped
yielding, where P2.8 deliberately keeps the registry row ageing toward the
freshness gate rather than dropping it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mf_faq.ingest.fact_card import FactCard
from mf_faq.logging_setup import get_logger
from mf_faq.retrieval.chunk import Chunk, chunk_cards
from mf_faq.retrieval.embedder import EmbedderProtocol, build_embedder
from mf_faq.settings import get_settings

log = get_logger(__name__)

#: Chroma rejects collection names shorter than 3 characters, among other rules.
#: Set here so a bad `settings.collection_name` fails at open, not mid-write.
_COSINE = {"hnsw:space": "cosine"}


@dataclass(frozen=True)
class RetrievedChunk:
    """One search hit: the served text, its metadata, and its cosine similarity."""

    chunk_id: str
    doc_id: str
    scheme_id: str
    doc_type: str
    text: str
    source_url: str
    source_as_of: str
    similarity: float

    @classmethod
    def from_chroma(
        cls, chunk_id: str, document: str, meta: dict, distance: float
    ) -> RetrievedChunk:
        return cls(
            chunk_id=chunk_id,
            doc_id=meta["doc_id"],
            scheme_id=meta["scheme_id"],
            doc_type=meta["doc_type"],
            text=document,
            source_url=meta["source_url"],
            source_as_of=meta["source_as_of"],
            # Cosine space: Chroma's distance is 1 - cos, so this inverts exactly.
            similarity=1.0 - float(distance),
        )


@dataclass
class SyncReport:
    """What one index sync did. `repaired` is the one to watch."""

    embedded: int = 0
    restamped: int = 0
    deleted: int = 0
    unchanged: int = 0
    skipped_conflicts: int = 0
    #: Chunks re-embedded because the index lacked them or held different text,
    #: while the registry considered them settled. Non-zero means the two
    #: artifacts had drifted — see the module docstring.
    repaired: int = 0
    repaired_ids: list[str] = field(default_factory=list)

    @property
    def written(self) -> int:
        return self.embedded + self.restamped

    def as_dict(self) -> dict[str, Any]:
        return {
            "embedded": self.embedded,
            "restamped": self.restamped,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "skipped_conflicts": self.skipped_conflicts,
            "repaired": self.repaired,
        }


class VectorStore:
    """A Chroma collection plus the embedder that writes into it."""

    def __init__(
        self,
        path: Path,
        collection_name: str,
        embedder: EmbedderProtocol,
        *,
        client: Any | None = None,
    ):
        self.path = path
        self.collection_name = collection_name
        self.embedder = embedder
        self._client = client
        self._collection = None

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(
        cls,
        *,
        path: Path | None = None,
        collection_name: str | None = None,
        embedder: EmbedderProtocol | None = None,
    ) -> VectorStore:
        settings = get_settings()
        return cls(
            path=path or settings.chroma_dir,
            collection_name=collection_name or settings.collection_name,
            embedder=embedder or build_embedder(),
        )

    @property
    def client(self):
        if self._client is None:
            import chromadb  # lazy: chromadb import is not cheap

            self.path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.path))
        return self._client

    @property
    def collection(self):
        if self._collection is None:
            self._collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata=_COSINE,
                embedding_function=None,  # we always supply vectors ourselves
            )
        return self._collection

    def count(self) -> int:
        return self.collection.count()

    # -- reads -------------------------------------------------------------

    def indexed_state(self, scheme_ids: Iterable[str] | None = None) -> dict[str, dict]:
        """`chunk_id` → stored metadata, optionally narrowed to some schemes.

        This is what makes the sync convergent: the question "what does the
        index actually hold?" is answered by the index, not inferred.
        """
        where = None
        if scheme_ids is not None:
            ids = list(scheme_ids)
            if not ids:
                return {}
            # Chroma rejects `$in` with a single element in some versions; the
            # equality form is also what the query path uses.
            where = {"scheme_id": ids[0]} if len(ids) == 1 else {"scheme_id": {"$in": ids}}
        result = self.collection.get(where=where, include=["metadatas"])
        return dict(zip(result["ids"], result["metadatas"], strict=True))

    def query(
        self, embedding: list[float], *, scheme_id: str | None, top_k: int
    ) -> list[RetrievedChunk]:
        """Nearest chunks, hard-filtered by scheme.

        `scheme_id` is the ARCH §6.1 mitigation-1 filter. Passing None is
        allowed but is *not* the ordinary path — `search.py` only does so for a
        deliberately unfiltered diagnostic, never for a user question.
        """
        if self.count() == 0:
            return []
        result = self.collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            where={"scheme_id": scheme_id} if scheme_id else None,
            include=["documents", "metadatas", "distances"],
        )
        return [
            RetrievedChunk.from_chroma(cid, doc, meta, dist)
            for cid, doc, meta, dist in zip(
                result["ids"][0],
                result["documents"][0],
                result["metadatas"][0],
                result["distances"][0],
                strict=True,
            )
        ]

    # -- writes ------------------------------------------------------------

    def upsert(self, chunks: list[Chunk]) -> None:
        """Embed and write. The only path that calls the embedding model.

        Note the two different texts: `embed_text` (scheme name stripped) makes
        the vector, `text` (the complete card) is what Chroma stores and what P4
        sends to the model and cites. See `chunk.py` for the measurement behind
        the split — and note that nothing a user sees is ever the stripped form.
        """
        if not chunks:
            return
        vectors = self.embedder.encode_documents([c.embed_text for c in chunks])
        self.collection.upsert(
            ids=[c.chunk_id for c in chunks],
            embeddings=vectors,
            documents=[c.text for c in chunks],
            metadatas=[c.metadata() for c in chunks],
        )

    def restamp(self, chunks: list[Chunk]) -> None:
        """Replace metadata without re-embedding (the P2.7 `REFRESHED` path).

        Chroma's `update` replaces a chunk's metadata wholesale, so the full
        dict is written — a partial update would drop `card_hash` and make the
        next sync think the chunk had diverged.
        """
        if not chunks:
            return
        self.collection.update(
            ids=[c.chunk_id for c in chunks],
            metadatas=[c.metadata() for c in chunks],
        )

    def delete(self, chunk_ids: list[str]) -> None:
        if chunk_ids:
            self.collection.delete(ids=chunk_ids)

    def drop(self) -> None:
        """Delete the collection outright. Used only by an explicit rebuild."""
        try:
            self.client.delete_collection(self.collection_name)
        except Exception:  # noqa: BLE001 - "not found" is success for a drop
            log.info("collection absent at drop", extra={"collection": self.collection_name})
        self._collection = None

    # -- reconciliation ----------------------------------------------------

    def sync(
        self,
        cards: list[FactCard],
        *,
        scope: Iterable[str],
        registry_doc_ids: Iterable[str],
        conflicted_doc_ids: Iterable[str] = (),
        decided_to_embed: Iterable[str] | None = None,
    ) -> SyncReport:
        """Converge the index on `cards`. Safe to run from any starting state.

        `scope`               scheme_ids this run covered — the only ones whose
                              index entries may be deleted.
        `registry_doc_ids`    doc_ids `documents` holds within scope. An index
                              entry with no registry row is removed.
        `conflicted_doc_ids`  I-09 quarantines: left exactly as they are.
        `decided_to_embed`    doc_ids this run's change detection asked to
                              embed. Supplied only so drift can be *named*:
                              anything embedded beyond this set is a repair.
                              Pass None (a full rebuild) to skip the accounting.
        """
        scope = list(scope)
        conflicted = set(conflicted_doc_ids)
        registry = set(registry_doc_ids)
        decided_to_embed = None if decided_to_embed is None else set(decided_to_embed)
        report = SyncReport()

        desired = {c.chunk_id: c for c in chunk_cards(cards) if c.doc_id not in conflicted}
        report.skipped_conflicts = len(conflicted)
        current = self.indexed_state(scope)

        to_embed: list[Chunk] = []
        to_restamp: list[Chunk] = []
        for chunk_id, chunk in desired.items():
            stored = current.get(chunk_id)
            # Two hashes, two reasons to rewrite: `card_hash` covers the served
            # document, `embed_hash` covers the vector. They move independently
            # — an alias added to sources.yaml changes only the second — so
            # checking one would leave the other stale. See `Chunk.embed_hash`.
            if (
                stored is None
                or stored.get("card_hash") != chunk.card_hash
                or stored.get("embed_hash") != chunk.embed_hash
            ):
                to_embed.append(chunk)
                # A repair is precisely "sync had to embed something this run's
                # change detection did not ask for" — i.e. the index disagreed
                # with a registry the run considered settled. Skipped entirely
                # when the caller supplies no decisions (a full rebuild, where
                # every chunk is embedded by design and nothing is drift).
                if decided_to_embed is not None and chunk.doc_id not in decided_to_embed:
                    report.repaired += 1
                    report.repaired_ids.append(chunk_id)
            elif any(stored.get(k) != v for k, v in chunk.metadata().items()):
                to_restamp.append(chunk)
            else:
                report.unchanged += 1

        stale_ids = [
            chunk_id
            for chunk_id, meta in current.items()
            if chunk_id not in desired
            and meta.get("doc_id") not in conflicted
            and meta.get("doc_id") not in registry
        ]

        self.upsert(to_embed)
        self.restamp(to_restamp)
        self.delete(stale_ids)

        report.embedded = len(to_embed)
        report.restamped = len(to_restamp)
        report.deleted = len(stale_ids)

        log.info("index synced", extra={"collection": self.collection_name, **report.as_dict()})
        if report.repaired:
            log.warning(
                "index drift repaired — the registry held facts the index did not",
                extra={"count": report.repaired, "chunk_ids": report.repaired_ids[:10]},
            )
        return report
