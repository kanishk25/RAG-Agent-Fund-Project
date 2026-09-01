"""FastAPI application (P1.5, P4.8).

Phase 1 shipped the skeleton: startup wiring, config validation, `GET /health`.
`POST /ask` arrives here in Phase 4 — the full guardrail chain from
`generation/pipeline.ask()`, wired behind one endpoint that cannot skip any
gate, because the endpoint itself contains none of the logic; it only calls
`ask()` and renders whatever `AskResponse` comes back.

**`Searcher` and `AnswerClient` are built once, lazily, and reused** — not per
request. Building a `Searcher` loads the embedding model (seconds, ~90MB);
building an `AnswerClient` is cheap on its own (the real Groq client and its
API-key check are deferred until the first `.generate()` call — see
`generation/answer.py`). Lazy rather than built at startup so `GET /health`
stays fast for a liveness probe that never asks a question.

**Every dependency is FastAPI-injected** (`Depends`), not read from a global
directly in the handler, so `tests/test_api.py` can override both with fakes
via `app.dependency_overrides` and exercise the whole HTTP path — request
parsing, response serialisation, the guardrail chain — without a live index or
a live Groq call.

**The endpoint itself fails closed.** Anything `ask()` does not already turn
into a graceful `AskResponse` — a bug, a KeyError, anything unanticipated — is
caught here and rendered as the same "temporarily unavailable" response a 429
or a malformed model reply gets. A user must never see a stack trace (P4.5b's
requirement, generalised to the whole endpoint, not just the Groq call).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from mf_faq import db, index_version
from mf_faq.generation.answer import AnswerClient, build_answer_client
from mf_faq.generation.pipeline import ask
from mf_faq.generation.render import AskResponse, render_service_unavailable
from mf_faq.guardrails.freshness import evaluate_freshness
from mf_faq.logging_setup import configure_logging, get_logger
from mf_faq.retrieval.search import Searcher, build_searcher
from mf_faq.settings import get_settings, get_sources, validate_all_configs

log = get_logger(__name__)

DISCLAIMER = "Facts-only. No investment advice."

_searcher: Searcher | None = None
_answer_client: AnswerClient | None = None


def get_searcher() -> Searcher:
    """Lazy singleton — overridden with a fake in `tests/test_api.py`."""
    global _searcher
    if _searcher is None:
        _searcher = build_searcher()
    return _searcher


def get_answer_client() -> AnswerClient:
    """Lazy singleton — overridden with a fake in `tests/test_api.py`."""
    global _answer_client
    if _answer_client is None:
        _answer_client = build_answer_client()
    return _answer_client


class HealthResponse(BaseModel):
    status: str
    index_sha: str
    index_committed_at: str | None
    documents: int
    schemes: int
    disclaimer: str


class FreshnessEntry(BaseModel):
    scheme_id: str
    doc_type: str
    source_as_of: str
    age: int
    unit: str
    verdict: str


class FreshnessResponse(BaseModel):
    documents: list[FreshnessEntry]
    stale_count: int
    disclaimer: str


class AskRequest(BaseModel):
    """Deliberately unconstrained beyond `str`. Q-12 (empty query) and Q-13
    (oversized query) are handled INSIDE `ask()` as graceful `AskResponse`s,
    not as pydantic validation errors — a 422 would be a different, less
    informative shape of "no" than every other refusal in this system."""

    question: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate configs and initialise storage BEFORE serving.

    Config errors must surface here — at boot, loudly — not at 23:30 during the
    scheduled run or, worse, on a user's question (P1 exit criterion).
    """
    settings = get_settings()
    configure_logging(settings.log_level)

    validate_all_configs()
    db.init_db(settings.registry_db)

    sources = get_sources()
    log.info(
        "startup complete",
        extra={
            "schemes": len(sources.schemes),
            "doc_types": len(sources.doc_types),
            "index_sha": index_version.index_sha(),
            "documents": db.document_count(settings.registry_db),
        },
    )
    yield
    log.info("shutdown")


app = FastAPI(
    title="Mutual Fund FAQ Assistant",
    description=DISCLAIMER,
    version="0.1.0",
    lifespan=lifespan,
)

# The Next.js frontend (frontend/, P7) is a separate deployable and calls this
# API cross-origin. Only GET/POST are ever needed and no cookies/credentials
# are used anywhere in this app, so `allow_credentials` stays False.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allow_origins,
    allow_origin_regex=get_settings().cors_allow_origin_regex,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness plus the identity of the loaded index.

    `index_sha` is the operationally important field: if the daily workflow is
    committing but this SHA never moves, the API is serving a stale checkout
    (the P6.10 failure mode).
    """
    settings = get_settings()
    sources = get_sources()
    return HealthResponse(
        status="ok",
        index_sha=index_version.index_sha(),
        index_committed_at=index_version.index_committed_at(),
        documents=db.document_count(settings.registry_db),
        schemes=len(sources.schemes),
        disclaimer=DISCLAIMER,
    )


@app.get("/freshness", response_model=FreshnessResponse)
def freshness() -> FreshnessResponse:
    """Per-`doc_type`, per-scheme `source_as_of` and staleness verdict,
    read straight from the committed registry (P6.8, ARCH §8.5).

    Reuses `guardrails.freshness.evaluate_freshness` rather than a second copy
    of the staleness rules — the verdict shown here for a given fact is
    exactly what a live query against that same fact would be gated on right
    now (P4.3), not an approximation of it.
    """
    settings = get_settings()
    sources = get_sources()
    entries = []
    for row in db.all_documents(settings.registry_db):
        check = evaluate_freshness(
            row["doc_type"], date.fromisoformat(row["source_as_of"]), sources=sources
        )
        entries.append(
            FreshnessEntry(
                scheme_id=row["scheme_id"],
                doc_type=row["doc_type"],
                source_as_of=row["source_as_of"],
                age=check.age,
                unit=check.unit,
                verdict=check.verdict.value,
            )
        )
    stale = sum(1 for e in entries if e.verdict != "fresh")
    return FreshnessResponse(documents=entries, stale_count=stale, disclaimer=DISCLAIMER)


@app.post("/ask", response_model=AskResponse)
def ask_endpoint(
    request: AskRequest,
    searcher: Searcher = Depends(get_searcher),  # noqa: B008 - the FastAPI DI idiom
    answer_client: AnswerClient = Depends(get_answer_client),  # noqa: B008
) -> AskResponse:
    """The full guardrail chain (ARCH §7). See the module docstring for why
    this function contains none of the logic itself."""
    try:
        return ask(request.question, searcher=searcher, answer_client=answer_client)
    except Exception:
        # Fail-closed at the outermost layer too — see the module docstring.
        # Never logs `request.question`: P-08 extends to every code path here,
        # not only the ones inside `ask()` that already know to omit it.
        log.exception("unexpected failure handling /ask")
        return render_service_unavailable()
