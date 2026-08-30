"""sentence-transformers wrapper (P3.3).

Three decisions, each of which the rest of the retrieval path relies on.

**1. Embeddings are L2-normalised, and the collection uses cosine space.**
Chroma's default distance is L2, on which the configured similarity floor (0.35)
would mean nothing — an L2 distance is unbounded and scale-dependent, so a floor
tuned on one embedding model silently changes meaning under another. With
normalised vectors and `hnsw:space: cosine`, Chroma's distance is `1 - cos`, so
`similarity = 1 - distance` lands in a real [-1, 1] and the floor is a number a
human can reason about. `store.py` sets the space; this module guarantees the
normalisation half of that bargain.

**2. The model is loaded lazily, once.**
Importing `sentence_transformers` pulls in torch — seconds of import time and
hundreds of MB. The ingest CLI must stay usable, and the API must still boot,
without paying that unless something actually embeds. So the import happens
inside the loader, not at module scope.

**3. Documents and queries go through the same encoder, explicitly.**
`all-MiniLM-L6-v2` is a symmetric model: it has no separate query prefix, and
adding one would degrade it. The two methods exist anyway so the call sites read
honestly and so an asymmetric model could later be swapped in at one place
rather than at every caller.

Batching is the caller-visible knob (`batch_size`). It matters less than it
looks — a full corpus rebuild is 35 chunks — but a batch of one per HTTP-style
round trip would still be the wrong default.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mf_faq.logging_setup import get_logger
from mf_faq.settings import get_settings

log = get_logger(__name__)

DEFAULT_BATCH_SIZE = 32

Vector = list[float]


@runtime_checkable
class EmbedderProtocol(Protocol):
    """What the rest of retrieval needs from an embedder.

    Exists so tests can inject a deterministic stand-in. Most of the retrieval
    suite is about filtering, syncing and floors — behaviour that must not
    depend on a 90 MB model download to be tested.
    """

    @property
    def dimension(self) -> int: ...

    def encode_documents(self, texts: list[str]) -> list[Vector]: ...

    def encode_query(self, text: str) -> Vector: ...


class SentenceTransformerEmbedder:
    """The real embedder. Loads `settings.embedding_model` on first use."""

    def __init__(self, model_name: str | None = None, *, batch_size: int = DEFAULT_BATCH_SIZE):
        self.model_name = model_name or get_settings().embedding_model
        self.batch_size = batch_size
        self._model = None

    def _load(self):
        if self._model is None:
            # Imported here, not at module scope: see decision 2 above.
            from sentence_transformers import SentenceTransformer

            log.info("loading embedding model", extra={"model": self.model_name})
            self._model = SentenceTransformer(self.model_name)
        return self._model

    @property
    def dimension(self) -> int:
        return int(self._load().get_sentence_embedding_dimension())

    @property
    def max_seq_length(self) -> int:
        """Input window in word-piece tokens. 256 for MiniLM — see `chunk.py`."""
        return int(self._load().max_seq_length)

    def _encode(self, texts: list[str]) -> list[Vector]:
        if not texts:
            return []
        vectors = self._load().encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,  # the cosine bargain — see decision 1
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [v.tolist() for v in vectors]

    def encode_documents(self, texts: list[str]) -> list[Vector]:
        vectors = self._encode(texts)
        log.info("embedded documents", extra={"count": len(vectors), "model": self.model_name})
        return vectors

    def encode_query(self, text: str) -> Vector:
        return self._encode([text])[0]


def build_embedder(model_name: str | None = None) -> SentenceTransformerEmbedder:
    return SentenceTransformerEmbedder(model_name)
