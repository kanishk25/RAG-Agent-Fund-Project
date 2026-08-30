"""The PII gate (P4.1, ARCH §7.1). Every case is an edge-cases.md `P-` id."""

from __future__ import annotations

import pytest
import yaml

from mf_faq.guardrails.pii import PIIKind, contains_pii, detect_pii

EVAL_DIR_REFUSAL = "eval/refusal.yaml"


class TestDetectsRealPII:
    def test_pan_inline_in_an_otherwise_valid_question(self):
        """P-01."""
        finding = detect_pii("my PAN ABCDE1234F, what's the NAV?")
        assert finding is not None
        assert finding.kind is PIIKind.PAN

    def test_pan_is_case_insensitive(self):
        assert detect_pii("pan: abcde1234f").kind is PIIKind.PAN

    def test_aadhaar_with_spaces(self):
        """P-02 — normalise whitespace before matching."""
        assert detect_pii("my aadhaar is 1234 5678 9012").kind is PIIKind.AADHAAR

    def test_aadhaar_with_no_separators(self):
        assert detect_pii("123456789012").kind is PIIKind.AADHAAR

    @pytest.mark.parametrize(
        "phone",
        ["+91 98765 43210", "98765-43210", "9876543210"],
    )
    def test_phone_with_various_separators(self, phone):
        """P-03 — strip separators before matching.

        A `+91` country code merges its digits with the number (the `+` is not
        a digit, so it does not break the run, but the two-digit code and the
        10-digit number together read as a 12-digit run) — that variant reports
        AADHAAR rather than PHONE. The label is observability only; every
        variant here blocks, which is the only thing that matters behaviourally.
        """
        finding = detect_pii(f"call me on {phone} about the fund")
        assert finding is not None
        assert finding.kind in {PIIKind.PHONE, PIIKind.ACCOUNT_NUMBER, PIIKind.AADHAAR}

    def test_a_plain_10_digit_run_is_phone(self):
        assert detect_pii("9876543210").kind is PIIKind.PHONE

    def test_email_address(self):
        """P-04, PS §5.2."""
        assert detect_pii("reach me at investor@example.com").kind is PIIKind.EMAIL

    def test_a_long_digit_run_that_is_neither_phone_nor_aadhaar_length(self):
        """P-05 — a folio-like number. Blocking is the safe error."""
        finding = detect_pii("my folio number is 12345678901234")
        assert finding is not None
        assert finding.kind is PIIKind.ACCOUNT_NUMBER

    def test_pii_anywhere_in_the_query_is_found(self):
        """P-07 — PII in an otherwise valid question triggers the same as bare PII."""
        assert contains_pii(
            "Call me on +91 98765 43210 with the expense ratio of "
            "Motilal Oswal Large and Midcap Fund"
        )


class TestDoesNotBlockLegitimateNumbers:
    """P-06 — the tight threshold this module exists to get right."""

    def test_a_6_digit_amfi_scheme_code_is_not_blocked(self):
        assert not contains_pii("the AMFI scheme code is 125497")

    def test_a_nav_value_is_not_blocked(self):
        assert not contains_pii("What is the NAV? It was 31.4782 yesterday.")

    def test_a_nav_with_more_decimal_digits_is_not_blocked(self):
        assert not contains_pii("NAV: 123.456789")

    def test_a_minimum_sip_amount_is_not_blocked(self):
        assert not contains_pii("the minimum SIP is 500 rupees")

    def test_comma_grouped_rupee_amounts_are_not_merged_across_the_comma(self):
        """Indian rupee grouping is not a PII separator — see the module docstring."""
        assert not contains_pii("the fund manages 1,00,00,000 rupees in assets")

    def test_a_plain_ordinary_question_has_no_pii(self):
        assert not contains_pii("What is the expense ratio of Motilal Oswal ELSS Tax Saver Fund?")

    def test_empty_string_has_no_pii(self):
        assert not contains_pii("")


class TestWholeQueryRejection:
    def test_finding_carries_no_matched_text_attribute(self):
        """P-08, structurally: there is no field to accidentally log."""
        finding = detect_pii("PAN ABCDE1234F")
        assert not hasattr(finding, "text")
        assert not hasattr(finding, "match")
        assert not hasattr(finding, "detail")
        assert finding.__dataclass_fields__.keys() == {"kind"}


class TestAgainstTheAuthoredEvalSet:
    def test_every_pii_case_in_refusal_yaml_is_detected(self):
        with open(EVAL_DIR_REFUSAL, encoding="utf-8") as fh:
            cases = yaml.safe_load(fh)["cases"]
        pii_cases = [c for c in cases if c["reason"] == "pii"]
        assert len(pii_cases) == 2  # guards against silently losing coverage
        for case in pii_cases:
            assert contains_pii(case["question"]), case["id"]
