"""API tests: `/health` (P1.5) and `/ask` (P4.8).

`/ask` is tested entirely through FastAPI's `dependency_overrides` — real
fakes for `Searcher` and `AnswerClient`, injected the same way the app itself
requests them (`Depends(get_searcher)`, `Depends(get_answer_client)`). This
exercises the real HTTP path (request parsing, `AskResponse` serialisation,
status codes) without a live index or a live Groq call. The guardrail chain's
own branch coverage lives in `tests/test_pipeline_generation.py`; this file is
about the HTTP layer around it.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from mf_faq.api.main import DISCLAIMER, app, get_answer_client, get_searcher
from mf_faq.generation.answer import FactAnswer, GenerationResult
from mf_faq.guardrails.freshness import IST
from mf_faq.retrieval.resolve import Resolution, SchemeMatch
from mf_faq.retrieval.search import SearchOutcome, SearchResult
from mf_faq.retrieval.store import RetrievedChunk


def _today_ist_iso() -> str:
    """Always 'today' — see test_pipeline_generation.py's copy of this helper
    for why a hardcoded past date here silently starts failing later."""
    return datetime.now(IST).date().isoformat()


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Point the registry at a temp path so tests never touch the committed index.
    from mf_faq.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MF_FAQ_REGISTRY_DB", str(tmp_path / "registry.db"))
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
    app.dependency_overrides.clear()


def _chunk(**over) -> RetrievedChunk:
    defaults = dict(
        chunk_id="mo_elss:nav#0",
        doc_id="mo_elss:nav",
        scheme_id="mo_elss",
        doc_type="nav",
        text="The latest declared NAV of Motilal Oswal ELSS Tax Saver Fund is 41.70 per unit.",
        source_url="https://groww.in/mutual-funds/x",
        source_as_of=_today_ist_iso(),
        similarity=0.9,
    )
    return RetrievedChunk(**{**defaults, **over})


class FakeSearcher:
    def __init__(self, result: SearchResult):
        self.result = result

    def search(self, query: str) -> SearchResult:
        return self.result


class FakeAnswerClient:
    def __init__(self, outcome):
        self.outcome = outcome

    def generate(self, question, chunks):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _ok_result(chunks: list[RetrievedChunk]) -> SearchResult:
    match = SchemeMatch(Resolution.RESOLVED, "mo_elss", ("mo_elss",), (), "test")
    return SearchResult(outcome=SearchOutcome.OK, chunks=chunks, scheme=match, floor=0.35)


def _generation(
    is_answerable=True, citation_index=1, answer="The NAV is 41.70."
) -> GenerationResult:
    fact = FactAnswer(is_answerable, None if is_answerable else "reason", answer, citation_index)
    return GenerationResult(answer=fact, prompt_tokens=100, completion_tokens=20, total_tokens=120)


@pytest.fixture
def wired(client):
    """Override the injected dependencies with a clean-answer happy path.

    Individual tests still override further where they need a different
    outcome — this fixture exists so most tests do not have to wire both
    dependencies from scratch.
    """
    app.dependency_overrides[get_searcher] = lambda: FakeSearcher(_ok_result([_chunk()]))
    app.dependency_overrides[get_answer_client] = lambda: FakeAnswerClient(_generation())
    yield client


def test_health_returns_200(client):
    assert client.get("/health").status_code == 200


def test_health_reports_index_identity(client):
    """P6.10 guard: if the workflow commits daily but this SHA never moves,
    the API is serving a stale checkout. Exposing it makes that visible."""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert isinstance(body["index_sha"], str) and body["index_sha"]
    assert body["schemes"] == 5


def test_health_carries_the_disclaimer(client):
    """PS §4.4 — the disclaimer is a product requirement, not decoration."""
    assert client.get("/health").json()["disclaimer"] == DISCLAIMER
    assert DISCLAIMER == "Facts-only. No investment advice."


def test_startup_validates_configs(client):
    """Config errors surface at boot. If validation had failed, the TestClient
    context manager would have raised before any request ran."""
    assert client.get("/health").status_code == 200


class TestFreshnessEndpoint:
    """P6.8 — `GET /freshness`, reading straight from the committed registry."""

    def test_empty_registry_returns_no_documents(self, client):
        body = client.get("/freshness").json()
        assert body["documents"] == []
        assert body["stale_count"] == 0
        assert body["disclaimer"] == DISCLAIMER

    def test_reports_a_fresh_and_a_stale_fact(self, client, tmp_path):
        from mf_faq import db

        registry_db = tmp_path / "registry.db"
        with db.session(registry_db) as conn:
            conn.execute(
                "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
                "content_hash, card_hash, source_as_of, fetched_at, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "mo_elss:nav",
                    "mo_elss",
                    "nav",
                    "https://groww.in/mutual-funds/x",
                    "h",
                    "c",
                    _today_ist_iso(),
                    "2026-08-30T12:00:00+05:30",
                    "ok",
                ),
            )
            conn.execute(
                "INSERT INTO documents (doc_id, scheme_id, doc_type, source_url, "
                "content_hash, card_hash, source_as_of, fetched_at, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "mo_elss:holdings",
                    "mo_elss",
                    "holdings",
                    "https://groww.in/mutual-funds/x",
                    "h",
                    "c",
                    "2026-01-01",  # long past the 45-day flag threshold
                    "2026-08-30T12:00:00+05:30",
                    "ok",
                ),
            )

        body = client.get("/freshness").json()
        by_doc_type = {d["doc_type"]: d for d in body["documents"]}
        assert by_doc_type["nav"]["verdict"] == "fresh"
        assert by_doc_type["holdings"]["verdict"] == "flag"
        assert body["stale_count"] == 1


class TestAskEndpoint:
    """P4.8 — the guardrail chain, reached over real HTTP.

    Branch coverage for the chain itself lives in
    `tests/test_pipeline_generation.py`; these tests are about the HTTP layer:
    status codes, request/response shape, and that the endpoint really is
    wired to `generation.pipeline.ask()` via the injected dependencies.
    """

    def test_a_clean_question_returns_200_and_an_answer(self, wired):
        response = wired.post(
            "/ask", json={"question": "What is the NAV of Motilal Oswal ELSS Tax Saver Fund?"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["answered"] is True
        assert "41.70" in body["text"]
        assert body["citation_url"] == "https://groww.in/mutual-funds/x"
        assert body["disclaimer"] == DISCLAIMER

    def test_a_refused_question_still_returns_200(self, client):
        """A refusal is a correct outcome (P4), not an HTTP error (P7.3)."""
        app.dependency_overrides[get_searcher] = lambda: FakeSearcher(_ok_result([_chunk()]))
        app.dependency_overrides[get_answer_client] = lambda: FakeAnswerClient(_generation())
        response = client.post(
            "/ask", json={"question": "Should I invest in Motilal Oswal ELSS Tax Saver Fund?"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["answered"] is False
        assert body["refusal_reason"] == "advisory_direct"

    def test_empty_question_is_a_graceful_refusal_not_a_422(self, wired):
        """Q-12 — a validation error, but rendered by ask(), not by pydantic."""
        response = wired.post("/ask", json={"question": ""})
        assert response.status_code == 200
        assert response.json()["refusal_reason"] == "empty_query"

    def test_missing_question_field_is_a_422(self, wired):
        """Genuinely malformed request bodies are still a pydantic error."""
        response = wired.post("/ask", json={})
        assert response.status_code == 422

    def test_an_unavailable_generation_client_returns_a_graceful_response(self, client):
        from mf_faq.generation.answer import Unavailable

        app.dependency_overrides[get_searcher] = lambda: FakeSearcher(_ok_result([_chunk()]))
        app.dependency_overrides[get_answer_client] = lambda: FakeAnswerClient(
            Unavailable("rate limited")
        )
        response = client.post(
            "/ask", json={"question": "What is the NAV of Motilal Oswal ELSS Tax Saver Fund?"}
        )
        assert response.status_code == 200
        assert response.json()["refusal_reason"] == "service_unavailable"

    def test_an_unexpected_exception_never_reaches_the_client_as_a_500(self, client):
        """The outermost fail-closed layer — see api/main.py's docstring."""

        class Boom:
            def search(self, query):
                raise RuntimeError("unexpected bug")

        app.dependency_overrides[get_searcher] = lambda: Boom()
        app.dependency_overrides[get_answer_client] = lambda: FakeAnswerClient(_generation())
        response = client.post(
            "/ask", json={"question": "What is the NAV of Motilal Oswal ELSS Tax Saver Fund?"}
        )
        assert response.status_code == 200
        assert response.json()["refusal_reason"] == "service_unavailable"

    def test_the_response_is_a_valid_ask_response_shape(self, wired):
        body = wired.post(
            "/ask", json={"question": "What is the NAV of Motilal Oswal ELSS Tax Saver Fund?"}
        ).json()
        assert set(body) == {
            "answered",
            "text",
            "citation_url",
            "source_as_of",
            "stale",
            "refusal_reason",
            "link",
            "disclaimer",
        }

    def test_searcher_and_answer_client_are_dependency_injected(self, client):
        """Proves the endpoint uses `Depends`, not a hardcoded global — the
        override actually has to take effect for this to pass."""
        called = {"search": False, "generate": False}

        class TrackingSearcher(FakeSearcher):
            def search(self, query):
                called["search"] = True
                return super().search(query)

        class TrackingAnswerClient(FakeAnswerClient):
            def generate(self, question, chunks):
                called["generate"] = True
                return super().generate(question, chunks)

        app.dependency_overrides[get_searcher] = lambda: TrackingSearcher(_ok_result([_chunk()]))
        app.dependency_overrides[get_answer_client] = lambda: TrackingAnswerClient(_generation())
        client.post(
            "/ask", json={"question": "What is the NAV of Motilal Oswal ELSS Tax Saver Fund?"}
        )
        assert called == {"search": True, "generate": True}
