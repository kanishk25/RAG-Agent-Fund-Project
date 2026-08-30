"""Post-generation validator (P4.6, ARCH §7.4).

The exit criterion this file exists for: "Injected bad LLM outputs (4
sentences / foreign citation / ungrounded number / advisory phrasing) are each
caught by the validator, verified by unit test." Every TestRejects* class below
is one of those four, injected directly — never provoked from a live model.
"""

from __future__ import annotations

import pytest

from mf_faq.generation.answer import FactAnswer
from mf_faq.guardrails.validator import (
    Check,
    ValidatedAnswer,
    ValidationFailure,
    count_sentences,
    validate,
)
from mf_faq.retrieval.store import RetrievedChunk


def _chunk(**over) -> RetrievedChunk:
    defaults = dict(
        chunk_id="mo_elss:expense_ratio#0",
        doc_id="mo_elss:expense_ratio",
        scheme_id="mo_elss",
        doc_type="expense_ratio",
        text="The total expense ratio (TER) of Motilal Oswal ELSS Tax Saver Fund Direct Growth "
        "is 0.97% per annum.",
        source_url="https://groww.in/mutual-funds/x",
        source_as_of="2026-08-28",
        similarity=0.9,
    )
    return RetrievedChunk(**{**defaults, **over})


def _answer(**over) -> FactAnswer:
    defaults = dict(is_answerable=True, refusal_reason=None, answer="", citation_index=1)
    return FactAnswer(**{**defaults, **over})


GOOD_ANSWER = "The expense ratio of Motilal Oswal ELSS Tax Saver Fund is 0.97% per annum."


class TestSentenceCounting:
    """G-07 — naive `.`-splitting is not good enough."""

    def test_one_plain_sentence(self):
        assert count_sentences("The NAV is 41.70 per unit.") == 1

    def test_rs_abbreviation_does_not_split(self):
        assert count_sentences("Minimum SIP is Rs. 500 per month.") == 1

    def test_decimal_point_does_not_split(self):
        assert count_sentences("The expense ratio is 0.62%.") == 1

    def test_multiple_decimals_do_not_split(self):
        assert count_sentences("NAV moved from 31.4782 to 31.9901 today.") == 1

    def test_genuinely_two_sentences_counts_two(self):
        assert count_sentences("The NAV is 41.70. It was declared on 2026-08-28.") == 2

    def test_genuinely_four_sentences_counts_four(self):
        text = "One. Two. Three. Four."
        assert count_sentences(text) == 4

    def test_trailing_whitespace_does_not_add_a_phantom_sentence(self):
        assert count_sentences("The NAV is 41.70 per unit.   ") == 1

    def test_empty_string_is_zero_sentences(self):
        assert count_sentences("") == 0


class TestRejectsFourSentences:
    """G-01 — no retry, straight refusal."""

    def test_four_sentences_fails_validation(self):
        answer = _answer(
            answer="Fact one. Fact two. Fact three. Fact four.",
            citation_index=1,
        )
        result = validate(answer, [_chunk()])
        assert isinstance(result, ValidationFailure)
        assert result.check == Check.SENTENCE_COUNT

    def test_exactly_three_sentences_passes_this_check(self):
        answer = _answer(answer="One fact. Two fact. Three fact.", citation_index=1)
        chunk = _chunk(text="One fact. Two fact. Three fact.")
        result = validate(answer, [chunk])
        assert not isinstance(result, ValidationFailure) or result.check != Check.SENTENCE_COUNT


class TestRejectsForeignCitation:
    """G-04 — citation_index must resolve within the retrieved set."""

    def test_citation_index_out_of_range_high(self):
        answer = _answer(answer=GOOD_ANSWER, citation_index=5)
        result = validate(answer, [_chunk()])
        assert isinstance(result, ValidationFailure)
        assert result.check == Check.CITATION_RANGE

    def test_citation_index_zero_is_out_of_range(self):
        """1-based indexing — 0 is not a valid position."""
        answer = _answer(answer=GOOD_ANSWER, citation_index=0)
        result = validate(answer, [_chunk()])
        assert isinstance(result, ValidationFailure)
        assert result.check == Check.CITATION_RANGE

    def test_negative_citation_index_is_out_of_range(self):
        answer = _answer(answer=GOOD_ANSWER, citation_index=-1)
        result = validate(answer, [_chunk()])
        assert isinstance(result, ValidationFailure)
        assert result.check == Check.CITATION_RANGE

    def test_citation_index_none_while_answerable_is_invalid(self):
        answer = _answer(answer=GOOD_ANSWER, citation_index=None)
        result = validate(answer, [_chunk()])
        assert isinstance(result, ValidationFailure)
        assert result.check == Check.CITATION_RANGE

    def test_a_valid_citation_resolves_the_correct_chunk(self):
        chunks = [_chunk(doc_id="a"), _chunk(doc_id="b")]
        answer = _answer(answer=GOOD_ANSWER, citation_index=2)
        result = validate(answer, chunks)
        assert isinstance(result, ValidatedAnswer)
        assert result.cited_chunk.doc_id == "b"


class TestRejectsUngroundedNumbers:
    """G-05 — a number in the answer that appears nowhere in the cited chunk."""

    def test_a_fabricated_number_fails_grounding(self):
        answer = _answer(
            answer="The expense ratio of Motilal Oswal ELSS Tax Saver Fund is 5.55% per annum.",
            citation_index=1,
        )
        result = validate(answer, [_chunk()])
        assert isinstance(result, ValidationFailure)
        assert result.check == Check.NUMERIC_GROUNDING

    def test_the_real_number_grounds_successfully(self):
        answer = _answer(answer=GOOD_ANSWER, citation_index=1)
        result = validate(answer, [_chunk()])
        assert isinstance(result, ValidatedAnswer)


class TestNumericGroundingNormalisation:
    """G-08 — ₹500 vs Rs 500 vs 500, and 0.62% vs 0.62 %, must be equivalent."""

    def test_rupee_symbol_is_ignored_by_normalisation(self):
        chunk = _chunk(text="The minimum SIP amount for the fund is ₹500.")
        answer = _answer(answer="The minimum SIP for the fund is Rs 500.", citation_index=1)
        assert isinstance(validate(answer, [chunk]), ValidatedAnswer)

    def test_percent_sign_spacing_is_ignored(self):
        chunk = _chunk(text="The TER of the fund is 0.62%.")
        answer = _answer(answer="The TER of the fund is 0.62 %.", citation_index=1)
        assert isinstance(validate(answer, [chunk]), ValidatedAnswer)

    def test_comma_grouped_amounts_ground_against_ungrouped_chunk_digits(self):
        chunk = _chunk(text="The fund manages assets of value 1234567 as disclosed.")
        answer = _answer(answer="The fund's assets are valued at 12,34,567.", citation_index=1)
        assert isinstance(validate(answer, [chunk]), ValidatedAnswer)

    def test_years_and_months_both_ground_because_the_card_states_both(self):
        """P2.6's design: fact_card.py renders BOTH forms for exactly this reason."""
        chunk = _chunk(
            doc_type="lock_in",
            text="Motilal Oswal ELSS Tax Saver Fund has a lock-in period of 3 years (36 months).",
        )
        for phrasing in ["The lock-in period is 3 years.", "The lock-in period is 36 months."]:
            answer = _answer(answer=phrasing, citation_index=1)
            assert isinstance(validate(answer, [chunk]), ValidatedAnswer), phrasing

    def test_the_date_in_the_answer_grounds_against_source_as_of(self):
        """The P0.9 NAV template states the declaration date verbatim."""
        chunk = _chunk(
            doc_type="nav",
            text="The latest declared NAV of the fund is ₹41.70 per unit.",
            source_as_of="2026-08-28",
        )
        answer = _answer(answer="The NAV was ₹41.70, as declared on 2026-08-28.", citation_index=1)
        assert isinstance(validate(answer, [chunk]), ValidatedAnswer)


class TestGroundingKnownGap:
    """G-06 — a real, accepted limit. Documented, not silently pretended fixed."""

    def test_a_number_from_the_wrong_attribute_of_the_same_chunk_still_grounds(self):
        """The validator cannot tell WHICH attribute a number belongs to —
        only that it appears somewhere in the cited chunk's text. A wrong but
        internally-consistent answer passes; only the golden set catches this.
        """
        chunk = _chunk(
            text="The exit load of the fund is 1% if redeemed within 365 days.",
        )
        # Genuinely wrong: quotes the exit-load percentage as if it were TER.
        answer = _answer(answer="The expense ratio of the fund is 1% per annum.", citation_index=1)
        result = validate(answer, [chunk])
        assert isinstance(result, ValidatedAnswer)  # accepted limitation, not a bug


class TestRejectsAdvisoryLanguage:
    """G-11 — a factually sourced answer phrased as a recommendation."""

    @pytest.mark.parametrize(
        "phrase",
        [
            "this low ratio is attractive for investors.",
            "you should consider this fund.",
            "we recommend this fund for its low cost.",
            "this is a good investment.",
            "it is recommended for long-term investors.",
        ],
    )
    def test_advisory_phrasing_is_rejected(self, phrase):
        chunk = _chunk()
        answer = _answer(answer=f"The TER is 0.97%; {phrase}", citation_index=1)
        result = validate(answer, [chunk])
        assert isinstance(result, ValidationFailure)
        assert result.check == Check.ADVISORY_LANGUAGE

    def test_plain_factual_language_is_not_flagged(self):
        answer = _answer(answer=GOOD_ANSWER, citation_index=1)
        assert isinstance(validate(answer, [_chunk()]), ValidatedAnswer)


class TestRejectsEmptyAnswerWhenAnswerable:
    """G-02."""

    def test_empty_answer_with_is_answerable_true_is_invalid(self):
        answer = _answer(answer="", citation_index=1)
        result = validate(answer, [_chunk()])
        assert isinstance(result, ValidationFailure)
        assert result.check == Check.ANSWER_NOT_EMPTY

    def test_whitespace_only_answer_is_invalid(self):
        answer = _answer(answer="   ", citation_index=1)
        result = validate(answer, [_chunk()])
        assert isinstance(result, ValidationFailure)
        assert result.check == Check.ANSWER_NOT_EMPTY


class TestValidateRejectsMisuseOnRefusals:
    def test_calling_validate_on_a_refusal_raises(self):
        """A refusal has no citation — validating it would be a caller bug."""
        answer = _answer(is_answerable=False, answer="", citation_index=None)
        with pytest.raises(ValueError, match="is_answerable=True"):
            validate(answer, [_chunk()])


class TestFooterDateGuaranteedByConstruction:
    """The fifth named check ('footer matches cited chunk') is not a sixth
    runtime comparison — see the module docstring. This proves why: there is
    only one source of the footer date once citation_range has passed."""

    def test_the_validated_answers_cited_chunk_is_the_only_date_source(self):
        chunks = [
            _chunk(doc_id="a", source_as_of="2026-08-01"),
            _chunk(doc_id="b", source_as_of="2026-08-28"),
        ]
        answer = _answer(answer=GOOD_ANSWER, citation_index=2)
        result = validate(answer, chunks)
        assert isinstance(result, ValidatedAnswer)
        # Whatever render.py does with the footer, it can only read this one
        # date — there is no second, model-derived date anywhere in the result.
        assert result.cited_chunk.source_as_of == "2026-08-28"
        assert not hasattr(result, "footer_date")
