"""`ask(query)` — the full guardrail chain, wired end to end (P4.8, ARCH §7).

```
PII gate -> deterministic intent -> scheme resolution + retrieval ->
freshness (per chunk) -> generation -> validation -> render
```

**Fail-closed at every branch.** Every stage either returns an `AskResponse`
directly (a refusal) or hands a narrower, already-checked value to the next
stage. There is no code path from "something unexpected happened" to "answer
anyway" — an unrecognised outcome, an exhausted retry, or a validator failure
all resolve to a refusal or a service-unavailable message, never to an
un-vetted answer reaching a user (P4's stated exit criterion: "no code path
can emit an answer without a citation drawn from retrieved chunks").

**Nothing before the PII gate is logged.** `query.strip()` is measured for
length before the PII check only because a length check reveals nothing about
content; the PII check itself runs before any other stage touches the text,
matching ARCH §7.1's "pre-everything" requirement literally — not "first
guardrail", first *anything*.

**Freshness filters chunks before generation ever sees them, not after.**
ARCH §7.3: "there is no point spending a call on a document that cannot
legally be quoted." A REFUSE-verdict chunk is dropped from the numbered
context outright — the model is never even shown it, let alone tempted to
cite it. A FLAG-verdict chunk is kept but marked, and an answer built from it
renders with a visible staleness note (P7.4) rather than silently reading as
current.
"""

from __future__ import annotations

from datetime import date, datetime

from mf_faq.generation.answer import AnswerClient, Unavailable, build_answer_client
from mf_faq.generation.prompts import PromptChunk
from mf_faq.generation.render import (
    AskResponse,
    render_ambiguous_refusal,
    render_answer,
    render_below_floor_refusal,
    render_empty_query,
    render_intent_refusal,
    render_model_refusal,
    render_no_facts_refusal,
    render_pii_refusal,
    render_query_too_long,
    render_service_unavailable,
    render_stale_refusal,
    render_unresolved_scheme_refusal,
    render_validation_failure_refusal,
)
from mf_faq.guardrails.freshness import FreshnessVerdict, evaluate_freshness
from mf_faq.guardrails.intent import classify
from mf_faq.guardrails.pii import detect_pii
from mf_faq.guardrails.validator import ValidationFailure, validate
from mf_faq.logging_setup import get_logger
from mf_faq.retrieval.resolve import resolve_scheme
from mf_faq.retrieval.search import Searcher, SearchOutcome, build_searcher
from mf_faq.schemas import SourcesConfig
from mf_faq.settings import get_settings, get_sources

log = get_logger(__name__)


def ask(
    query: str,
    *,
    searcher: Searcher | None = None,
    answer_client: AnswerClient | None = None,
    sources: SourcesConfig | None = None,
    now: datetime | None = None,
) -> AskResponse:
    """Answer one question, or refuse — the only two possible outcomes.

    `searcher` and `answer_client` are injectable so the whole chain is tested
    without a live index or a live Groq call (mirrors `ingest/pipeline.run`'s
    injectable `Fetcher`). `now` passes straight through to the freshness gate
    (`guardrails.freshness.evaluate_freshness`) for the same reason — a test
    can pin "today" to prove a specific fact has gone stale, rather than
    waiting for a fixture's real date to age.
    """
    settings = get_settings()
    sources = sources or get_sources()

    stripped = query.strip()
    if not stripped:
        log.info("query rejected", extra={"decision": "empty_query"})
        return render_empty_query()
    if len(stripped) > settings.max_query_chars:
        # Q-13: rejected before spending a token on it — never logs the query
        # itself, only its length, which reveals nothing about content.
        log.info("query rejected", extra={"decision": "query_too_long", "length": len(stripped)})
        return render_query_too_long(settings.max_query_chars)

    # PII gate: pre-EVERYTHING (ARCH §7.1). Nothing above this line inspected
    # content; nothing below it may run before this check has passed.
    finding = detect_pii(stripped)
    if finding is not None:
        log.info("query rejected", extra={"decision": "pii", "kind": finding.kind.value})
        return render_pii_refusal()

    # Deterministic intent pre-filter (P4.2) — free, no tokens, un-jailbreakable.
    verdict = classify(stripped)
    if verdict is not None:
        match = resolve_scheme(stripped)
        scheme_id = match.scheme_id if match.resolved else None
        log.info(
            "query refused",
            extra={"decision": verdict.reason.value, "matched_pattern": verdict.matched},
        )
        return render_intent_refusal(verdict.reason, scheme_id=scheme_id)

    # Scheme resolution + retrieval (P3), each empty outcome named (P3.6).
    searcher = searcher or build_searcher()
    result = searcher.search(stripped)

    if result.outcome is SearchOutcome.EMPTY_INDEX:
        log.warning("query against an empty index")
        return render_service_unavailable()
    if result.outcome is SearchOutcome.SCHEME_AMBIGUOUS:
        log.info("query refused", extra={"decision": "ambiguous_scheme"})
        candidates = [sources.scheme(sid) for sid in result.candidates]
        return render_ambiguous_refusal(candidates)
    if result.outcome is SearchOutcome.SCHEME_UNRESOLVED:
        log.info("query refused", extra={"decision": "scheme_unresolved"})
        return render_unresolved_scheme_refusal(sources.schemes)
    if result.outcome is SearchOutcome.NO_FACTS_FOR_SCHEME:
        log.info("query refused", extra={"decision": "no_facts_for_scheme"})
        return render_no_facts_refusal(sources.scheme(result.scheme_id))
    if result.outcome is SearchOutcome.BELOW_FLOOR:
        log.info("query refused", extra={"decision": "below_floor"})
        scheme = sources.scheme(result.scheme_id) if result.scheme_id else None
        return render_below_floor_refusal(scheme)

    scheme = sources.scheme(result.scheme_id)

    # Freshness gate (P4.3) — runs BEFORE generation. A REFUSE-verdict chunk
    # never reaches the model at all; a FLAG-verdict one is kept and marked.
    kept: list[PromptChunk] = []
    for chunk in result.chunks:
        check = evaluate_freshness(
            chunk.doc_type, date.fromisoformat(chunk.source_as_of), now=now, sources=sources
        )
        if check.verdict is FreshnessVerdict.REFUSE:
            continue
        kept.append(PromptChunk(chunk, stale=check.verdict is FreshnessVerdict.FLAG))

    if not kept:
        log.info("query refused", extra={"decision": "stale_content"})
        return render_stale_refusal(scheme, result.chunks[0].doc_type)

    # Generation (P4.5) — the only stage that spends a Groq token.
    answer_client = answer_client or build_answer_client()
    try:
        generated = answer_client.generate(stripped, kept)
    except Unavailable:
        log.error("generation unavailable")
        return render_service_unavailable()

    log.info(
        "generation complete",
        extra={
            "prompt_tokens": generated.prompt_tokens,
            "completion_tokens": generated.completion_tokens,
            "total_tokens": generated.total_tokens,
            "is_answerable": generated.answer.is_answerable,
        },
    )

    if not generated.answer.is_answerable:
        log.info("model refused", extra={"decision": "not_answerable"})
        return render_model_refusal()

    # Validation (P4.6) — no code path from here to `render_answer` skips this.
    validated = validate(generated.answer, [item.chunk for item in kept])
    if isinstance(validated, ValidationFailure):
        log.warning(
            "validation failed",
            extra={"check": validated.check, "detail": validated.detail},
        )
        return render_validation_failure_refusal(validated)

    cited_stale = kept[generated.answer.citation_index - 1].stale
    return render_answer(validated, stale=cited_stale)
