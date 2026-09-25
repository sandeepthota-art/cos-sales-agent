from datetime import datetime, timedelta, timezone

from app.entities.dates import _add_business_days, classify_follow_up_timing, find_date_phrase, resolve_date_phrase

_NOW = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)  # a Sunday


def test_resolve_date_phrase_returns_none_for_no_phrase():
    assert resolve_date_phrase(None, _NOW) == (None, None)
    assert resolve_date_phrase("", _NOW) == (None, None)


def test_resolve_date_phrase_handles_explicit_month_day_as_stated():
    resolved, date_type = resolve_date_phrase("by June 5th", _NOW)
    assert date_type == "stated"
    assert resolved.month == 6
    assert resolved.day == 5
    assert resolved.year == 2027  # June 5 already passed in the reference year, rolls to next year


def test_resolve_date_phrase_explicit_date_on_the_same_day_does_not_roll_to_next_year():
    # A same-day reference ("June 5th" sent on June 5th at 2pm) must resolve to THIS
    # year's June 5th, not next year's -- comparing full timestamps (candidate normalized
    # to midnight vs. reference_now's real time-of-day) would wrongly treat today as
    # "already passed" and roll forward a year.
    reference = datetime(2026, 6, 5, 14, 0, tzinfo=timezone.utc)
    resolved, date_type = resolve_date_phrase("by June 5th", reference)
    assert date_type == "stated"
    assert resolved.year == 2026
    assert resolved.month == 6
    assert resolved.day == 5


def test_resolve_date_phrase_handles_tomorrow_as_inferred():
    resolved, date_type = resolve_date_phrase("let's talk tomorrow", _NOW)
    assert date_type == "inferred"
    assert resolved.date() == (_NOW.date().replace(day=_NOW.day + 1))


def test_resolve_date_phrase_handles_weekday_as_inferred():
    resolved, date_type = resolve_date_phrase("can we sync next Friday", _NOW)
    assert date_type == "inferred"
    assert resolved.weekday() == 4  # Friday


def test_resolve_date_phrase_handles_vague_phrase_as_window():
    resolved, date_type = resolve_date_phrase("sometime end of month", _NOW)
    assert resolved is None
    assert date_type == "window"


def test_resolve_date_phrase_falls_back_to_window_for_unrecognized_phrase():
    resolved, date_type = resolve_date_phrase("whenever works", _NOW)
    assert resolved is None
    assert date_type == "window"


def test_resolve_date_phrase_handles_relative_duration_in_weeks_as_inferred():
    # Spec worked example: email dated 2026-09-17, "in 2 weeks" -> 2026-10-01
    reference = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)
    resolved, date_type = resolve_date_phrase("We will meet in 2 weeks.", reference)
    assert date_type == "inferred"
    assert resolved.date().isoformat() == "2026-10-01"


def test_resolve_date_phrase_handles_relative_duration_in_days_with_digit():
    resolved, date_type = resolve_date_phrase("The meeting is in 10 days.", _NOW)
    assert date_type == "inferred"
    assert resolved == _NOW + timedelta(days=10)


def test_resolve_date_phrase_handles_relative_duration_with_spelled_out_number():
    resolved, date_type = resolve_date_phrase("Let's catch up in two weeks.", _NOW)
    assert date_type == "inferred"
    assert resolved == _NOW + timedelta(days=14)


def test_resolve_date_phrase_handles_relative_time_in_hours_as_inferred():
    resolved, date_type = resolve_date_phrase("Remind me in 2 hours if I don't hear back.", _NOW)
    assert date_type == "inferred"
    assert resolved == _NOW + timedelta(hours=2)


def test_resolve_date_phrase_handles_relative_time_in_minutes_as_inferred():
    resolved, date_type = resolve_date_phrase("Follow up in 30 minutes.", _NOW)
    assert date_type == "inferred"
    assert resolved == _NOW + timedelta(minutes=30)


def test_resolve_date_phrase_relative_time_in_hours_never_lands_in_the_past():
    resolved, _ = resolve_date_phrase("Let's follow up on this in 3 hours.", _NOW)
    assert resolved > _NOW


def test_resolve_date_phrase_relative_duration_in_days_still_resolves_correctly_alongside_hours():
    # Regression guard: adding hour/minute support must not change day/week resolution.
    resolved, date_type = resolve_date_phrase("Please follow up with John in 2 days.", _NOW)
    assert date_type == "inferred"
    assert resolved == _NOW + timedelta(days=2)


def test_resolve_date_phrase_recognizes_next_month_explicitly_as_window():
    resolved, date_type = resolve_date_phrase("We should meet sometime next month.", _NOW)
    assert resolved is None
    assert date_type == "window"


def test_resolve_date_phrase_falls_back_to_window_for_invalid_day_of_month():
    # "June 45th" and "Feb 30" are plausible substrings of real email prose -- not
    # adversarial input -- and datetime.replace(day=...) raises ValueError for either.
    # A date was clearly mentioned but can't be pinned to a specific day, so this must
    # fall back to the same (None, "window") semantic as any other unresolvable phrase,
    # not raise and fail the whole email.
    assert resolve_date_phrase("by June 45th", _NOW) == (None, "window")
    assert resolve_date_phrase("by Feb 30", _NOW) == (None, "window")


def test_find_date_phrase_returns_first_recognized_expression():
    # The weekday pattern matches only the weekday word itself, not a preceding "next" --
    # resolve_date_phrase always computes the *next* occurrence of that weekday regardless,
    # so the "next" prefix carries no additional information it needs.
    assert find_date_phrase("We will meet in 2 weeks.") == "in 2 weeks"
    assert find_date_phrase("Let's meet next Friday.") == "Friday"
    assert find_date_phrase("We can meet tomorrow.") == "tomorrow"
    assert find_date_phrase("The meeting is in 10 days.") == "in 10 days"
    assert find_date_phrase("Remind me in 2 hours.") == "in 2 hours"
    assert find_date_phrase("Follow up in 30 minutes.") == "in 30 minutes"


def test_find_date_phrase_returns_none_when_nothing_recognized():
    assert find_date_phrase("Just checking in, no dates mentioned.") is None


def test_resolve_date_phrase_recognizes_bare_next_week_as_window():
    # "next week" (no specific day attached) doesn't pin an exact date the way "next
    # Monday" does -- must be preserved as evidence and marked "window", never
    # resolved to an invented exact day.
    assert find_date_phrase("Let's reconnect next week.") == "next week"
    resolved, date_type = resolve_date_phrase("Let's reconnect next week.", _NOW)
    assert resolved is None
    assert date_type == "window"


# --- _add_business_days ---------------------------------------------------------------


def test_add_business_days_skips_weekend_entirely():
    thursday = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc)  # Thursday
    assert _add_business_days(thursday, 1).date().isoformat() == "2026-09-18"  # Friday
    assert _add_business_days(thursday, 2).date().isoformat() == "2026-09-21"  # Monday (skips Sat/Sun)
    assert _add_business_days(thursday, 3).date().isoformat() == "2026-09-22"  # Tuesday


def test_add_business_days_from_a_friday_skips_straight_to_monday():
    friday = datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)
    assert _add_business_days(friday, 1).date().isoformat() == "2026-09-21"  # Monday


# --- classify_follow_up_timing (BRD 6.4) -----------------------------------------------


def test_classify_follow_up_timing_returns_nothing_for_non_chased_classes():
    for commitment_class in ("mine", "theirs", "recap"):
        audience, earliest, latest = classify_follow_up_timing(
            commitment_class=commitment_class, date_type="stated", committed_date=_NOW,
            is_internal_counterparty=True,
        )
        assert (audience, earliest, latest) == (None, None, None)


def test_classify_follow_up_timing_internal_chases_one_to_two_days_after_deadline():
    audience, earliest, latest = classify_follow_up_timing(
        commitment_class="owed_to_me", date_type="stated", committed_date=_NOW,
        is_internal_counterparty=True,
    )
    assert audience == "internal"
    assert earliest == _NOW + timedelta(days=1)
    assert latest == _NOW + timedelta(days=2)


def test_classify_follow_up_timing_client_fixed_date_waits_two_to_three_business_days():
    thursday = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc)
    audience, earliest, latest = classify_follow_up_timing(
        commitment_class="owed_to_me", date_type="stated", committed_date=thursday,
        is_internal_counterparty=False,
    )
    assert audience == "client_fixed_date"
    assert earliest == _add_business_days(thursday, 2)
    assert latest == _add_business_days(thursday, 3)


def test_classify_follow_up_timing_client_open_window_never_invents_a_timestamp():
    audience, earliest, latest = classify_follow_up_timing(
        commitment_class="owed_to_me", date_type="window", committed_date=None,
        is_internal_counterparty=False,
    )
    assert audience == "client_open_window"
    assert earliest is None
    assert latest is None


def test_classify_follow_up_timing_internal_with_no_anchor_date_yields_no_timestamp():
    audience, earliest, latest = classify_follow_up_timing(
        commitment_class="owed_to_me", date_type="window", committed_date=None,
        is_internal_counterparty=True,
    )
    assert audience == "internal"
    assert earliest is None
    assert latest is None
