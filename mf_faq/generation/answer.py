"""One Groq call: structured output, 429 backoff, and the failure path (P4.5,
P4.5b, ARCH §7.5).

**On the free tier, a 429 is a normal operating condition, not an exceptional
one** (ARCH §15.4: 30 RPM / 8,000 TPM). This module treats it that way: a rate
limit retries with backoff honouring the API's own `retry-after` header, and
only raises `Unavailable` once retries are exhausted — which the caller turns
into a plain "temporarily unavailable" message, never a stack trace (edge case
list, "Groq 429" row).

**Every parse failure also raises `Unavailable`, never a crash and never a
silent wrong answer.** Groq's strict mode is a constrained decoder, not a
guarantee against every possible failure — the connection can drop mid-stream,
the response can be truncated, or (G-09) a provider-side change to how
`strict` is honoured could someday let a schema violation through. Treating
"the JSON didn't parse the way the schema promised" as anything other than an
immediate refusal-worthy failure would mean trusting a payload nothing has
validated.

**Nothing here decides whether the answer is CORRECT.** This module's job ends
at "a well-formed `FactAnswer` came back, and here is what it cost." Whether
`citation_index` actually points at a real chunk, whether the sentence count
and grounding hold up — that is `guardrails/validator.py`, deliberately kept
separate, because a well-formed response and a trustworthy one are different
questions asked by different code.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from mf_faq.generation.prompts import (
    FACT_ANSWER_SCHEMA,
    FACTS_ONLY_SYSTEM_PROMPT,
    RESPONSE_SCHEMA_NAME,
    PromptChunk,
    render_user_message,
)
from mf_faq.logging_setup import get_logger
from mf_faq.settings import get_settings

log = get_logger(__name__)

#: Backoff used when the API gives no `retry-after` at all — should not
#: normally happen, but a missing header must not mean "retry instantly".
DEFAULT_BACKOFF_SECONDS = 2.0
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class FactAnswer:
    """The model's structured response, parsed and nothing more.

    Every field here is advisory until `guardrails/validator.py` has looked at
    it — see the module docstring. `render.py` never reads `answer` or
    `refusal_reason` from a REFUSED response (`is_answerable=False`); they are
    parsed here only so a refusal can be logged with the model's own stated
    reason for debugging.
    """

    is_answerable: bool
    refusal_reason: str | None
    answer: str
    citation_index: int | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> FactAnswer:
        return cls(
            is_answerable=bool(payload["is_answerable"]),
            refusal_reason=payload["refusal_reason"],
            answer=payload["answer"],
            citation_index=payload["citation_index"],
        )


@dataclass(frozen=True)
class GenerationResult:
    """A successful call: the parsed answer plus the API's OWN token count.

    `prompt_tokens`/`completion_tokens`/`total_tokens` come straight from
    `response.usage` — the exit criterion "measured tokens per request
    recorded" (4.4) is this field, not an estimate. `prompts.estimate_tokens`
    is for sizing the prompt while writing it; this is the number that
    actually happened.
    """

    answer: FactAnswer
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class Unavailable(Exception):
    """Generation could not complete: retries exhausted, a connection/API
    error, or a response that did not parse into a `FactAnswer`. The caller
    (`generation/pipeline.py`) renders this as a plain "temporarily
    unavailable" message — it is never allowed to reach a user as a stack
    trace, and it is never treated as "no answer exists" the way an ordinary
    `is_answerable=False` refusal is."""


class ChatCompletions(Protocol):
    """The one Groq SDK call this module makes. Narrowed to a Protocol so
    tests inject a minimal fake rather than a real `groq.Groq` client."""

    def create(self, **kwargs: Any) -> Any: ...


def _retry_after_seconds(exc: Exception) -> float | None:
    """Extract `Retry-After` from a Groq API error's HTTP response, if present."""
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response else None
    if header is None:
        return None
    try:
        return max(0.0, float(header))
    except (TypeError, ValueError):
        return None


class AnswerClient:
    """Wraps one Groq chat-completions endpoint with the project's retry policy.

    `completions` and `sleep` are injectable so every code path here — success,
    429-then-success, 429-exhausted, malformed JSON, a connection error — is
    tested without touching the network or a real clock (mirrors the pattern
    `ingest/fetch.Fetcher` already uses for the same reason).
    """

    def __init__(
        self,
        completions: ChatCompletions | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
    ):
        settings = get_settings()
        self._completions = completions
        self.model = model or settings.model
        self.temperature = settings.temperature if temperature is None else temperature
        self.max_output_tokens = max_output_tokens or settings.max_output_tokens
        self.max_attempts = max_attempts
        self.sleep = sleep

    @property
    def completions(self) -> ChatCompletions:
        if self._completions is None:
            # Imported here, not at module scope: importing `groq` must not be
            # a cost paid by every caller of this module (e.g. the retrieval
            # CLI never needs it), and building a real client requires the API
            # key, which ingestion callers of this package never have.
            from groq import Groq

            self._completions = Groq(api_key=get_settings().require_groq_key()).chat.completions
        return self._completions

    def generate(self, question: str, chunks: list[PromptChunk]) -> GenerationResult:
        """One question, its numbered context, one structured answer."""
        messages = [
            {"role": "system", "content": FACTS_ONLY_SYSTEM_PROMPT},
            {"role": "user", "content": render_user_message(question, chunks)},
        ]
        response = self._call_with_backoff(messages)

        raw = response.choices[0].message.content
        try:
            payload = json.loads(raw)
            answer = FactAnswer.from_payload(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            # G-09/G-10: strict mode is a constrained decoder, not a proof.
            # Treated identically whether the JSON was malformed (G-10) or
            # well-formed but shaped wrong (G-09) — both are "nothing here can
            # be trusted", not "let's guess which field is usable".
            log.error("model output did not parse", extra={"error": type(exc).__name__})
            raise Unavailable("model returned an unparseable response") from exc

        usage = response.usage
        return GenerationResult(
            answer=answer,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
        )

    def _call_with_backoff(self, messages: list[dict[str, str]]) -> Any:
        from groq import APIConnectionError, APIStatusError, RateLimitError

        attempt = 0
        while True:
            attempt += 1
            try:
                return self.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=self.max_output_tokens,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": RESPONSE_SCHEMA_NAME,
                            "strict": True,
                            "schema": FACT_ANSWER_SCHEMA,
                        },
                    },
                )
            except RateLimitError as exc:
                if attempt >= self.max_attempts:
                    log.error(
                        "groq rate limit — retries exhausted",
                        extra={"attempts": attempt},
                    )
                    raise Unavailable("rate limited") from exc
                delay = _retry_after_seconds(exc) or DEFAULT_BACKOFF_SECONDS * attempt
                log.warning(
                    "groq rate limited — backing off",
                    extra={"attempt": attempt, "delay_seconds": delay},
                )
                self.sleep(delay)
            except (APIConnectionError, APIStatusError) as exc:
                log.error("groq API error", extra={"error": type(exc).__name__})
                raise Unavailable(f"groq API error: {type(exc).__name__}") from exc


def build_answer_client(**kwargs: Any) -> AnswerClient:
    return AnswerClient(**kwargs)
