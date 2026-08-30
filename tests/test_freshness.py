"""The freshness gate (P4.3). Pure date arithmetic — no LLM calls (eval.md §5.4)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from mf_faq.guardrails.freshness import (
    IST,
    FreshnessVerdict,
    business_days_elapsed,
    evaluate_freshness,
)


def _at(iso: str) -> datetime:
    """A fixed 'now', always given as an IST-local moment for readability."""
    return datetime.fromisoformat(iso).replace(tzinfo=IST)


class TestBusinessDaysElapsed:
    def test_same_day_is_zero(self):
        assert business_days_elapsed(date(2026, 8, 26), date(2026, 8, 26)) == 0

    def test_consecutive_weekdays_is_one(self):
        # Tue -> Wed
        assert business_days_elapsed(date(2026, 8, 25), date(2026, 8, 26)) == 1

    def test_friday_to_monday_is_one(self):
        """The weekend case: Friday's NAV is still 1 business day old on Monday."""
        friday = date(2026, 8, 28)
        monday = date(2026, 8, 31)
        assert business_days_elapsed(friday, monday) == 1

    def test_thursday_to_monday_is_two(self):
        """A missed Friday run: 2 business days elapsed, correctly over a 1-day floor."""
        thursday = date(2026, 8, 27)
        monday = date(2026, 8, 31)
        assert business_days_elapsed(thursday, monday) == 2

    def test_end_before_start_is_zero(self):
        assert business_days_elapsed(date(2026, 8, 26), date(2026, 8, 20)) == 0


class TestNavRefusesPastOneBusinessDay:
    """PS §9 item 6 / ARCH §7.3: nav is 1 business day, past_max_age=refuse."""

    def test_todays_nav_is_fresh(self):
        check = evaluate_freshness("nav", date(2026, 8, 26), now=_at("2026-08-26T10:00:00"))
        assert check.verdict is FreshnessVerdict.FRESH

    def test_one_business_day_old_is_still_fresh_the_inclusive_boundary(self):
        """F-03 — exactly at the boundary. Friday's NAV on Monday morning."""
        check = evaluate_freshness(
            "nav", date(2026, 8, 28), now=_at("2026-08-31T09:00:00")
        )  # Fri -> Mon
        assert check.verdict is FreshnessVerdict.FRESH
        assert check.age == 1

    def test_two_business_days_old_refuses(self):
        """F-08: a missed run. Thursday's NAV is stale by Monday."""
        check = evaluate_freshness(
            "nav", date(2026, 8, 27), now=_at("2026-08-31T09:00:00")
        )  # Thu -> Mon
        assert check.verdict is FreshnessVerdict.REFUSE
        assert check.age == 2

    def test_a_weekday_gap_of_one_calendar_day_but_two_business_days_refuses(self):
        # Wed source, Fri now would be 2 calendar days AND 2 business days —
        # confirm the business-day unit is actually being used, not calendar days.
        check = evaluate_freshness(
            "nav", date(2026, 8, 26), now=_at("2026-08-28T09:00:00")
        )  # Wed -> Fri
        assert check.age == 2
        assert check.verdict is FreshnessVerdict.REFUSE


class TestFlaggedDocTypes:
    """expense_ratio, holdings: 45 days flag. exit_load: 400 days flag."""

    @pytest.mark.parametrize("doc_type", ["expense_ratio", "holdings"])
    def test_within_45_days_is_fresh(self, doc_type):
        check = evaluate_freshness(doc_type, date(2026, 7, 20), now=_at("2026-08-26T10:00:00"))
        assert check.verdict is FreshnessVerdict.FRESH

    @pytest.mark.parametrize("doc_type", ["expense_ratio", "holdings"])
    def test_exactly_45_days_is_fresh_the_inclusive_boundary(self, doc_type):
        check = evaluate_freshness(doc_type, date(2026, 7, 12), now=_at("2026-08-26T10:00:00"))
        assert check.age == 45
        assert check.verdict is FreshnessVerdict.FRESH

    @pytest.mark.parametrize("doc_type", ["expense_ratio", "holdings"])
    def test_46_days_flags_not_refuses(self, doc_type):
        check = evaluate_freshness(doc_type, date(2026, 7, 11), now=_at("2026-08-26T10:00:00"))
        assert check.age == 46
        assert check.verdict is FreshnessVerdict.FLAG

    def test_exit_load_tolerates_400_days(self):
        check = evaluate_freshness("exit_load", date(2025, 7, 22), now=_at("2026-08-26T10:00:00"))
        assert check.age == 400
        assert check.verdict is FreshnessVerdict.FRESH

    def test_exit_load_flags_past_400_days(self):
        check = evaluate_freshness("exit_load", date(2025, 7, 21), now=_at("2026-08-26T10:00:00"))
        assert check.age == 401
        assert check.verdict is FreshnessVerdict.FLAG


class TestExemptDocTypes:
    """min_sip, lock_in, benchmark: PS §9 item 9 — no staleness protection."""

    @pytest.mark.parametrize("doc_type", ["min_sip", "lock_in", "benchmark"])
    def test_always_fresh_no_matter_how_old(self, doc_type):
        check = evaluate_freshness(doc_type, date(2015, 1, 1), now=_at("2026-08-26T10:00:00"))
        assert check.verdict is FreshnessVerdict.FRESH
        assert check.max_age_days is None

    @pytest.mark.parametrize("doc_type", ["min_sip", "lock_in", "benchmark"])
    def test_a_future_date_still_refuses_despite_being_exempt(self, doc_type):
        """F-04 overrides even the exempt policy — see the module docstring."""
        check = evaluate_freshness(doc_type, date(2026, 9, 1), now=_at("2026-08-26T10:00:00"))
        assert check.verdict is FreshnessVerdict.REFUSE
        assert check.is_future_dated


class TestFutureDatedFact:
    """F-04 — a parser bug defence-in-depth layer; P2.5 is the primary control."""

    def test_a_future_nav_refuses(self):
        check = evaluate_freshness("nav", date(2026, 9, 1), now=_at("2026-08-26T10:00:00"))
        assert check.verdict is FreshnessVerdict.REFUSE
        assert check.is_future_dated

    def test_a_future_flagged_doc_type_refuses_not_flags(self):
        check = evaluate_freshness("holdings", date(2026, 9, 1), now=_at("2026-08-26T10:00:00"))
        assert check.verdict is FreshnessVerdict.REFUSE


class TestTimezonePinning:
    """F-07 — dates compare in IST, never naive UTC."""

    def test_now_defaults_to_utc_but_is_converted_to_ist(self):
        # 2026-08-26 23:00 UTC is 2026-08-27 04:30 IST — the NEXT calendar day.
        now_utc = datetime(2026, 8, 26, 23, 0, tzinfo=UTC)
        check = evaluate_freshness("nav", date(2026, 8, 26), now=now_utc)
        assert check.age == 1  # one IST calendar day has passed, not zero

    def test_naive_datetime_is_rejected(self):
        """A naive `now` is exactly the ambiguity F-07 exists to close off."""
        with pytest.raises(ValueError, match="timezone-aware"):
            evaluate_freshness("nav", date(2026, 8, 26), now=datetime(2026, 8, 27, 10, 0))

    def test_a_moment_just_before_ist_midnight_does_not_roll_the_date_early(self):
        # 2026-08-26 18:00 UTC = 2026-08-26 23:30 IST — still the 26th in IST.
        now_utc = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
        check = evaluate_freshness("nav", date(2026, 8, 26), now=now_utc)
        assert check.age == 0


class TestUnknownDocTypeFailsLoud:
    def test_raises_keyerror(self):
        with pytest.raises(KeyError):
            evaluate_freshness("riskometer", date(2026, 8, 26), now=_at("2026-08-26T10:00:00"))
