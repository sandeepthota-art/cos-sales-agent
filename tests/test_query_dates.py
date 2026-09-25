# tests/test_query_dates.py
"""Phase 22A: deterministic date-range resolution. Every test injects an explicit
reference_datetime -- nothing here ever touches datetime.now()."""
from datetime import datetime, timezone

import pytest

from app.query.dates import resolve_date_range
from app.query.schemas import DateRangeKind

# A Wednesday, deliberately not the timezone's midnight, to catch any code that
# silently assumes reference_datetime is already local midnight.
_REF_UTC = datetime(2026, 3, 11, 20, 30, tzinfo=timezone.utc)  # 2026-03-12 02:00 IST


def test_today_resolves_to_local_midnight_window():
    r = resolve_date_range(DateRangeKind.TODAY, _REF_UTC, "Asia/Kolkata")
    assert r.start.isoformat() == "2026-03-12T00:00:00+05:30"
    assert r.end.isoformat() == "2026-03-13T00:00:00+05:30"


def test_today_in_a_different_timezone_gives_a_different_window():
    r_utc = resolve_date_range(DateRangeKind.TODAY, _REF_UTC, "UTC")
    r_ist = resolve_date_range(DateRangeKind.TODAY, _REF_UTC, "Asia/Kolkata")
    assert r_utc.start != r_ist.start  # same instant, different local calendar day


def test_tomorrow_and_yesterday_are_one_day_offset_from_today():
    today = resolve_date_range(DateRangeKind.TODAY, _REF_UTC, "Asia/Kolkata")
    tomorrow = resolve_date_range(DateRangeKind.TOMORROW, _REF_UTC, "Asia/Kolkata")
    yesterday = resolve_date_range(DateRangeKind.YESTERDAY, _REF_UTC, "Asia/Kolkata")
    assert tomorrow.start == today.end
    assert yesterday.end == today.start


def test_this_week_starts_on_monday():
    r = resolve_date_range(DateRangeKind.THIS_WEEK, _REF_UTC, "Asia/Kolkata")
    assert r.start.weekday() == 0
    assert (r.end - r.start).days == 7


def test_next_week_and_last_week_are_seven_days_offset():
    this_week = resolve_date_range(DateRangeKind.THIS_WEEK, _REF_UTC, "Asia/Kolkata")
    next_week = resolve_date_range(DateRangeKind.NEXT_WEEK, _REF_UTC, "Asia/Kolkata")
    last_week = resolve_date_range(DateRangeKind.LAST_WEEK, _REF_UTC, "Asia/Kolkata")
    assert (next_week.start - this_week.start).days == 7
    assert (this_week.start - last_week.start).days == 7


def test_this_month_and_next_month():
    r = resolve_date_range(DateRangeKind.THIS_MONTH, _REF_UTC, "Asia/Kolkata")
    assert r.start.day == 1 and r.start.month == 3
    assert r.end.day == 1 and r.end.month == 4

    nxt = resolve_date_range(DateRangeKind.NEXT_MONTH, _REF_UTC, "Asia/Kolkata")
    assert nxt.start.month == 4 and nxt.end.month == 5


def test_december_next_month_rolls_over_to_january_next_year():
    ref = datetime(2026, 12, 15, tzinfo=timezone.utc)
    r = resolve_date_range(DateRangeKind.NEXT_MONTH, ref, "UTC")
    assert r.start.year == 2027 and r.start.month == 1


def test_upcoming_is_open_ended_forward():
    r = resolve_date_range(DateRangeKind.UPCOMING, _REF_UTC, "UTC")
    assert r.start == _REF_UTC
    assert r.end is None


def test_overdue_is_open_ended_backward():
    r = resolve_date_range(DateRangeKind.OVERDUE, _REF_UTC, "UTC")
    assert r.start is None
    assert r.end == _REF_UTC


def test_recent_is_a_bounded_backward_window():
    r = resolve_date_range(DateRangeKind.RECENT, _REF_UTC, "UTC")
    assert r.start is not None and r.end == _REF_UTC
    assert (r.end - r.start).days == 7


def test_latest_and_all_time_have_no_bounds():
    latest = resolve_date_range(DateRangeKind.LATEST, _REF_UTC, "UTC")
    all_time = resolve_date_range(DateRangeKind.ALL_TIME, _REF_UTC, "UTC")
    assert latest.start is None and latest.end is None
    assert all_time.start is None and all_time.end is None


def test_between_requires_explicit_bounds():
    start, end = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc)
    r = resolve_date_range(DateRangeKind.BETWEEN, _REF_UTC, "UTC", explicit_start=start, explicit_end=end)
    assert r.start == start and r.end == end


def test_between_without_bounds_raises():
    with pytest.raises(ValueError, match="BETWEEN requires"):
        resolve_date_range(DateRangeKind.BETWEEN, _REF_UTC, "UTC")


def test_before_requires_explicit_end():
    end = datetime(2026, 2, 1, tzinfo=timezone.utc)
    r = resolve_date_range(DateRangeKind.BEFORE, _REF_UTC, "UTC", explicit_end=end)
    assert r.start is None and r.end == end
    with pytest.raises(ValueError, match="BEFORE requires"):
        resolve_date_range(DateRangeKind.BEFORE, _REF_UTC, "UTC")


def test_after_requires_explicit_start():
    start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    r = resolve_date_range(DateRangeKind.AFTER, _REF_UTC, "UTC", explicit_start=start)
    assert r.start == start and r.end is None
    with pytest.raises(ValueError, match="AFTER requires"):
        resolve_date_range(DateRangeKind.AFTER, _REF_UTC, "UTC")
