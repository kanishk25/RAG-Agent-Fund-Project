"""System prompt, schema, and numbered context (P4.4)."""

from __future__ import annotations

import jsonschema
import pytest

from mf_faq.generation.prompts import (
    FACT_ANSWER_SCHEMA,
    FACTS_ONLY_SYSTEM_PROMPT,
    PromptChunk,
    estimate_tokens,
    render_context,
    render_user_message,
)
from mf_faq.retrieval.store import RetrievedChunk


def _chunk(**over) -> RetrievedChunk:
    defaults = dict(
        chunk_id="mo_elss:nav#0",
        doc_id="mo_elss:nav",
        scheme_id="mo_elss",
        doc_type="nav",
        text="The latest declared NAV of Motilal Oswal ELSS Tax Saver Fund is ₹41.70 per unit.",
        source_url="https://groww.in/mutual-funds/x",
        source_as_of="2026-08-28",
        similarity=0.9,
    )
    return RetrievedChunk(**{**defaults, **over})


class TestSchemaIsValidStrictJsonSchema:
    def test_schema_itself_is_valid(self):
        jsonschema.Draft202012Validator.check_schema(FACT_ANSWER_SCHEMA)

    def test_additional_properties_forbidden(self):
        assert FACT_ANSWER_SCHEMA["additionalProperties"] is False

    def test_every_property_is_required(self):
        """Strict mode has no optional fields (ARCH §7.5)."""
        assert set(FACT_ANSWER_SCHEMA["required"]) == set(FACT_ANSWER_SCHEMA["properties"])

    def test_nullable_fields_use_a_type_union_not_optionality(self):
        assert FACT_ANSWER_SCHEMA["properties"]["refusal_reason"]["type"] == ["string", "null"]
        assert FACT_ANSWER_SCHEMA["properties"]["citation_index"]["type"] == ["integer", "null"]

    def test_a_valid_answerable_response_validates(self):
        sample = {
            "is_answerable": True,
            "refusal_reason": None,
            "answer": "The expense ratio is 0.92%.",
            "citation_index": 1,
        }
        jsonschema.validate(sample, FACT_ANSWER_SCHEMA)

    def test_a_valid_refusal_response_validates(self):
        sample = {
            "is_answerable": False,
            "refusal_reason": "advisory question",
            "answer": "",
            "citation_index": None,
        }
        jsonschema.validate(sample, FACT_ANSWER_SCHEMA)

    def test_a_missing_required_field_is_invalid(self):
        sample = {"is_answerable": True, "answer": "x", "citation_index": 1}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(sample, FACT_ANSWER_SCHEMA)

    def test_an_unlisted_property_is_invalid(self):
        sample = {
            "is_answerable": True,
            "refusal_reason": None,
            "answer": "x",
            "citation_index": 1,
            "extra_field": "not allowed",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(sample, FACT_ANSWER_SCHEMA)


class TestSystemPromptIsTerseAndMeasured:
    """4.4 — 'write it terse and measure its token count.'"""

    def test_the_estimate_is_recorded_so_growth_is_visible(self):
        # A regression guard, not a design target: if this creeps far past its
        # current size, that growth should be a deliberate choice, not drift.
        assert estimate_tokens(FACTS_ONLY_SYSTEM_PROMPT) < 500

    def test_no_few_shot_examples(self):
        """ARCH §7.5: examples earn their tokens only if evaluation proves it."""
        assert "example" not in FACTS_ONLY_SYSTEM_PROMPT.lower()

    def test_estimate_tokens_is_a_positive_heuristic(self):
        assert estimate_tokens("") == 1  # never zero — an empty prompt still costs something
        assert estimate_tokens("a" * 400) == 100


class TestRenderContext:
    def test_chunks_are_numbered_from_one_in_order(self):
        chunks = [PromptChunk(_chunk(chunk_id="a")), PromptChunk(_chunk(chunk_id="b"))]
        context = render_context(chunks)
        assert context.index("[1]") < context.index("[2]")

    def test_context_carries_source_url_and_date(self):
        context = render_context([PromptChunk(_chunk())])
        assert "source_url: https://groww.in/mutual-funds/x" in context
        assert "source_as_of: 2026-08-28" in context

    def test_a_stale_flagged_chunk_carries_a_visible_note(self):
        context = render_context([PromptChunk(_chunk(), stale=True)])
        assert "NOTE" in context
        assert "freshness" in context.lower()

    def test_a_fresh_chunk_carries_no_stale_note(self):
        context = render_context([PromptChunk(_chunk(), stale=False)])
        assert "NOTE" not in context

    def test_empty_context_renders_without_error(self):
        assert render_context([]) == ""


class TestRenderUserMessage:
    def test_carries_both_the_question_and_the_context(self):
        message = render_user_message("What is the NAV?", [PromptChunk(_chunk())])
        assert "What is the NAV?" in message
        assert "[1]" in message
