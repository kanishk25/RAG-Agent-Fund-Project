"""Shared offline harness for tests that drive the real ingestion stack.

Per ARCH §15.7 nothing here touches the live site: the Phase 0 fixtures are
wrapped back into a `__NEXT_DATA__` page and served through
`httpx.MockTransport`, so fetch → normalise → parse → cards runs exactly as it
does in production, against bytes we control.

Lives in conftest rather than in one test module because P2.8, P2.9 and later
P3/P6 all need the same harness, and a second copy would be a second thing to
keep in step with the page shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mf_faq.ingest.fetch import Fetcher
from mf_faq.settings import get_settings, get_sources

FIXTURES = Path(__file__).parent / "fixtures" / "groww"


@pytest.fixture(autouse=True, scope="session")
def _never_write_the_committed_index(tmp_path_factory):
    """Redirect the default index path away from `data/chroma` for the suite.

    ARCH §8.2 commits the index to git, so a test that writes to the configured
    directory edits a published artifact — and it happens by accident rather
    than intent: `tests/test_cli.py` drives the ingest CLI, the CLI now syncs
    the index, and any run that omits `--chroma-dir` lands on the real one. It
    was caught by deleting `data/chroma` and watching the suite recreate it with
    35 fixture-derived chunks.

    **Set through the environment, not by mutating the cached `Settings`.**
    `test_api.py` calls `get_settings.cache_clear()`, so a patched instance is
    discarded partway through the session and every later test quietly gets the
    real path back. That is exactly how the first version of this guard passed
    file-by-file and still leaked on a full run. An env var is re-read by every
    `Settings` ever constructed, cleared cache or not.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("MF_FAQ_CHROMA_DIR", str(tmp_path_factory.mktemp("chroma_default")))
    get_settings.cache_clear()
    yield
    mp.undo()
    get_settings.cache_clear()


ROBOTS = "User-agent: *\nDisallow: /dashboard/\n"

# A real scheme page is ~400 KB; the fetcher rejects anything under 2 KB as a
# block page (I-01/I-02), so the wrapper has to clear that bar.
_PADDING = "pad " * 600


def groww_payload(scheme_id: str) -> dict:
    """The `mfServerSideData` payload captured in Phase 0."""
    return json.loads((FIXTURES / f"{scheme_id}.json").read_text(encoding="utf-8"))


def groww_page_html(data: dict) -> str:
    """Wrap a payload back into the page shape `normalise()` expects."""
    blob = json.dumps({"props": {"pageProps": {"mfServerSideData": data}}})
    return (
        "<html><body>"
        + _PADDING
        + f'<script id="__NEXT_DATA__" type="application/json">{blob}</script>'
        + "</body></html>"
    )


@pytest.fixture
def groww_fetcher():
    """Factory for a `Fetcher` serving the fixtures offline.

    fetcher = groww_fetcher()                                   # all healthy
    fetcher = groww_fetcher(payloads={"mo_elss": mutated})      # altered data
    fetcher = groww_fetcher(fail={"mo_elss": 404})              # HTTP failure
    """
    sources = get_sources()
    lookup = {str(s.url): s.scheme_id for s in sources.schemes}

    def make(
        *,
        payloads: dict[str, dict] | None = None,
        fail: dict[str, int] | None = None,
        bodies: dict[str, str] | None = None,
    ) -> Fetcher:
        payloads, fail, bodies = payloads or {}, fail or {}, bodies or {}

        def route(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text=ROBOTS)
            scheme_id = lookup[str(request.url)]
            if scheme_id in fail:
                return httpx.Response(fail[scheme_id], text="nope")
            if scheme_id in bodies:
                return httpx.Response(200, text=bodies[scheme_id])
            return httpx.Response(
                200, text=groww_page_html(payloads.get(scheme_id) or groww_payload(scheme_id))
            )

        return Fetcher(
            fact_domain="groww.in",
            user_agent="mf-faq-bot/1.0 (+https://example.test/repo)",
            crawl_delay_seconds=0.0,
            max_retries=0,
            client=httpx.Client(transport=httpx.MockTransport(route), follow_redirects=True),
            sleep=lambda _s: None,  # never actually sleep in tests
        )

    return make


# --------------------------------------------------------------------------
# Retrieval harness (P3)
# --------------------------------------------------------------------------
# The retrieval suite is mostly about filtering, floors and reconciliation —
# behaviour that must be testable without downloading a 90 MB model. So a fake
# embedder stands in everywhere except the recall measurement, which is about
# the real model and says so.


class FakeEmbedder:
    """Deterministic bag-of-words embedder over a hashed 64-dim space.

    Not random vectors: hashing tokens into buckets means texts sharing words
    score high and unrelated texts score near zero, so similarity *ordering* is
    realistic enough to exercise the floor and the scheme filter. `hashlib` is
    used rather than `hash()` because Python randomises string hashing per
    process, and a test that passes only sometimes is worse than no test.
    """

    dimension = 64

    def _vector(self, text: str) -> list[float]:
        import hashlib
        import math
        import re

        vec = [0.0] * self.dimension
        for token in re.findall(r"[a-z0-9]+", text.casefold()):
            bucket = int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dimension
            vec[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def encode_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def encode_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def corpus_cards() -> list:
    """The real 35 fact cards, parsed from the Phase 0 fixtures. No network."""
    from mf_faq.ingest.fact_card import render_cards
    from mf_faq.ingest.parse import parse_facts

    sources = get_sources()
    cards = []
    for scheme in sources.schemes:
        facts, _ = parse_facts(
            groww_payload(scheme.scheme_id),
            scheme_id=scheme.scheme_id,
            source_url=str(scheme.url),
            doc_types=scheme.extract,
        )
        cards.extend(render_cards(facts, scheme))
    return cards


@pytest.fixture
def vector_store(tmp_path, fake_embedder):
    """A fresh on-disk Chroma collection per test."""
    from mf_faq.retrieval.store import VectorStore

    store = VectorStore(
        path=tmp_path / "chroma",
        collection_name="mf_facts_test",
        embedder=fake_embedder,
    )
    yield store


@pytest.fixture
def populated_store(vector_store, corpus_cards):
    """`vector_store` with all 35 cards indexed."""
    from mf_faq.retrieval.chunk import chunk_cards

    vector_store.upsert(chunk_cards(corpus_cards))
    return vector_store
