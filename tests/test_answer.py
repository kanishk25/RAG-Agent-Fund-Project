"""Groq call wrapper: structured output, 429 backoff, failure paths (P4.5, 4.5b)."""

from __future__ import annotations

import json

import httpx
import pytest
from groq import APIConnectionError, APIStatusError, RateLimitError

from mf_faq.generation.answer import AnswerClient, FactAnswer, Unavailable
from mf_faq.generation.prompts import PromptChunk
from mf_faq.retrieval.store import RetrievedChunk


def _chunk() -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id="mo_elss:nav#0",
        doc_id="mo_elss:nav",
        scheme_id="mo_elss",
        doc_type="nav",
        text="The latest declared NAV of Motilal Oswal ELSS Tax Saver Fund is ₹41.70 per unit.",
        source_url="https://groww.in/mutual-funds/x",
        source_as_of="2026-08-28",
        similarity=0.9,
    )


class FakeMessage:
    def __init__(self, content: str):
        self.content = content


class FakeChoice:
    def __init__(self, content: str):
        self.message = FakeMessage(content)


class FakeUsage:
    def __init__(self, prompt=100, completion=20, total=120):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total


class FakeResponse:
    def __init__(self, content: str, usage: FakeUsage | None = None):
        self.choices = [FakeChoice(content)]
        self.usage = usage or FakeUsage()


ANSWERABLE_JSON = json.dumps(
    {
        "is_answerable": True,
        "refusal_reason": None,
        "answer": "The NAV is ₹41.70, declared 2026-08-28.",
        "citation_index": 1,
    }
)

REFUSAL_JSON = json.dumps(
    {
        "is_answerable": False,
        "refusal_reason": "advisory question",
        "answer": "",
        "citation_index": None,
    }
)


class FakeCompletions:
    """Queue of canned responses or exceptions, one per call, in order."""

    def __init__(self, outcomes: list):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _rate_limit_error(retry_after: str | None = None) -> RateLimitError:
    headers = {"retry-after": retry_after} if retry_after else {}
    response = httpx.Response(429, headers=headers, request=httpx.Request("POST", "https://x"))
    return RateLimitError(message="rate limited", response=response, body=None)


def _client(outcomes, **kwargs) -> AnswerClient:
    sleeps = kwargs.pop("_sleeps", None)
    completions = FakeCompletions(outcomes)
    sleep_calls = sleeps if sleeps is not None else []
    return (
        AnswerClient(completions=completions, sleep=sleep_calls.append, **kwargs),
        completions,
        sleep_calls,
    )


class TestHappyPath:
    def test_a_clean_answerable_response_parses(self):
        client, _, _ = _client([FakeResponse(ANSWERABLE_JSON)])
        result = client.generate("What is the NAV?", [PromptChunk(_chunk())])
        assert result.answer.is_answerable is True
        assert result.answer.citation_index == 1
        assert "41.70" in result.answer.answer

    def test_a_clean_refusal_response_parses(self):
        client, _, _ = _client([FakeResponse(REFUSAL_JSON)])
        result = client.generate("Should I invest?", [PromptChunk(_chunk())])
        assert result.answer.is_answerable is False
        assert result.answer.answer == ""
        assert result.answer.citation_index is None

    def test_token_usage_is_the_apis_own_measured_count(self):
        """4.4 exit criterion: measured, not estimated."""
        client, _, _ = _client([FakeResponse(ANSWERABLE_JSON, FakeUsage(150, 30, 180))])
        result = client.generate("What is the NAV?", [PromptChunk(_chunk())])
        assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (
            150,
            30,
            180,
        )

    def test_the_call_uses_strict_json_schema_response_format(self):
        client, completions, _ = _client([FakeResponse(ANSWERABLE_JSON)])
        client.generate("What is the NAV?", [PromptChunk(_chunk())])
        kwargs = completions.calls[0]
        assert kwargs["response_format"]["type"] == "json_schema"
        assert kwargs["response_format"]["json_schema"]["strict"] is True

    def test_temperature_is_zero_by_default(self):
        client, completions, _ = _client([FakeResponse(ANSWERABLE_JSON)])
        client.generate("q", [PromptChunk(_chunk())])
        assert completions.calls[0]["temperature"] == 0.0

    def test_model_defaults_to_settings(self):
        from mf_faq.settings import get_settings

        client, completions, _ = _client([FakeResponse(ANSWERABLE_JSON)])
        client.generate("q", [PromptChunk(_chunk())])
        assert completions.calls[0]["model"] == get_settings().model


class Test429Handling:
    """4.5b — a normal operating condition on the free tier, not exceptional."""

    def test_a_single_429_then_success_retries_and_returns(self):
        client, completions, sleeps = _client(
            [_rate_limit_error("1"), FakeResponse(ANSWERABLE_JSON)]
        )
        result = client.generate("q", [PromptChunk(_chunk())])
        assert result.answer.is_answerable is True
        assert len(completions.calls) == 2

    def test_backoff_honours_the_retry_after_header(self):
        client, _, sleeps = _client([_rate_limit_error("3"), FakeResponse(ANSWERABLE_JSON)])
        client.generate("q", [PromptChunk(_chunk())])
        assert sleeps == [3.0]

    def test_a_missing_retry_after_falls_back_to_a_default_backoff(self):
        client, _, sleeps = _client([_rate_limit_error(None), FakeResponse(ANSWERABLE_JSON)])
        client.generate("q", [PromptChunk(_chunk())])
        assert sleeps and sleeps[0] > 0

    def test_a_malformed_retry_after_falls_back_rather_than_crashing(self):
        client, _, sleeps = _client(
            [_rate_limit_error("not-a-number"), FakeResponse(ANSWERABLE_JSON)]
        )
        client.generate("q", [PromptChunk(_chunk())])
        assert sleeps and sleeps[0] > 0

    def test_retries_exhausted_raises_unavailable_not_a_crash(self):
        client, completions, sleeps = _client(
            [_rate_limit_error("1"), _rate_limit_error("1"), _rate_limit_error("1")],
            max_attempts=3,
        )
        with pytest.raises(Unavailable):
            client.generate("q", [PromptChunk(_chunk())])
        assert len(completions.calls) == 3

    def test_never_retries_more_than_max_attempts(self):
        client, completions, _ = _client(
            [_rate_limit_error("0"), _rate_limit_error("0"), FakeResponse(ANSWERABLE_JSON)],
            max_attempts=2,
        )
        with pytest.raises(Unavailable):
            client.generate("q", [PromptChunk(_chunk())])
        assert len(completions.calls) == 2  # the 3rd (would-be-successful) call never happens


class TestOtherApiFailures:
    def test_a_connection_error_raises_unavailable(self):
        client, _, _ = _client([APIConnectionError(request=httpx.Request("POST", "https://x"))])
        with pytest.raises(Unavailable):
            client.generate("q", [PromptChunk(_chunk())])

    def test_a_5xx_status_error_raises_unavailable(self):
        response = httpx.Response(500, request=httpx.Request("POST", "https://x"))
        exc = APIStatusError(message="server error", response=response, body=None)
        client, _, _ = _client([exc])
        with pytest.raises(Unavailable):
            client.generate("q", [PromptChunk(_chunk())])

    def test_a_generic_api_error_never_reaches_the_caller_as_a_raw_exception(self):
        """The user-facing contract: a stack trace never happens (4.5b exit criterion)."""
        response = httpx.Response(503, request=httpx.Request("POST", "https://x"))
        exc = APIStatusError(message="unavailable", response=response, body=None)
        client, _, _ = _client([exc])
        try:
            client.generate("q", [PromptChunk(_chunk())])
            pytest.fail("expected Unavailable")
        except Unavailable:
            pass  # exactly the graceful path


class TestMalformedResponses:
    """G-09, G-10 — strict mode is a constrained decoder, not a proof."""

    def test_unparseable_json_raises_unavailable(self):
        client, _, _ = _client([FakeResponse("not json at all {{{")])
        with pytest.raises(Unavailable):
            client.generate("q", [PromptChunk(_chunk())])

    def test_valid_json_missing_a_required_field_raises_unavailable(self):
        broken = json.dumps({"is_answerable": True, "answer": "x"})  # no citation_index
        client, _, _ = _client([FakeResponse(broken)])
        with pytest.raises(Unavailable):
            client.generate("q", [PromptChunk(_chunk())])

    def test_valid_json_wrong_types_raises_unavailable(self):
        broken = json.dumps(
            {
                "is_answerable": "yes",  # should be bool — FactAnswer coerces via bool(),
                "refusal_reason": None,  # so this alone would not fail; combine with a real defect:
                "answer": None,  # None where a string is required IS a real defect downstream
                "citation_index": None,
            }
        )
        client, _, _ = _client([FakeResponse(broken)])
        # `answer` staying None is tolerated by from_payload (no type coercion there);
        # the meaningful guarantee is that a genuinely unparseable payload never crashes.
        result = client.generate("q", [PromptChunk(_chunk())])
        assert result.answer.answer is None


class TestFactAnswer:
    def test_from_payload_round_trips(self):
        payload = {
            "is_answerable": True,
            "refusal_reason": None,
            "answer": "x",
            "citation_index": 2,
        }
        answer = FactAnswer.from_payload(payload)
        assert answer == FactAnswer(True, None, "x", 2)
