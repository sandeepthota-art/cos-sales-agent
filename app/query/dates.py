"""Phase 22A: deterministic date-RANGE resolution for the query layer.

Deliberately separate from app.entities.dates.resolve_date_phrase, which resolves a
single POINT date out of free-text email prose during ingestion (a different
problem). This module resolves a named RANGE ("today", "this week", "overdue", ...)
against an explicit reference_datetime -- never datetime.now() directly -- exactly
mirroring app.calendar.detector's existing reference_now/timezone injection pattern,
so query resolution stays deterministic and test-injectable (Section F).
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.query.schemas import DateRange, DateRangeKind

_RECENT_WINDOW_DAYS = 7


def _local_midnight(reference_datetime: datetime, tz: ZoneInfo, day_offset: int = 0) -> datetime:
    local = reference_datetime.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=day_offset)


def resolve_date_range(
    kind: DateRangeKind,
    reference_datetime: datetime,
    timezone: str,
    explicit_start: datetime | None = None,
    explicit_end: datetime | None = None,
) -> DateRange:
    """Resolves one named range into concrete [start, end) bounds in the given
    timezone. BETWEEN/BEFORE/AFTER require explicit_start/explicit_end (whichever
    the shape needs) -- raises ValueError rather than silently guessing a bound the
    caller didn't supply. UPCOMING/OVERDUE/ALL_TIME are intentionally open-ended
    (None on the side that has no natural bound); LATEST has no range at all -- it's
    a sort+limit instruction the retrieval layer applies, not a window, so both
    start and end are None.
    """
    tz = ZoneInfo(timezone)

    if kind == DateRangeKind.TODAY:
        start = _local_midnight(reference_datetime, tz)
        end = start + timedelta(days=1)
    elif kind == DateRangeKind.TOMORROW:
        start = _local_midnight(reference_datetime, tz, day_offset=1)
        end = start + timedelta(days=1)
    elif kind == DateRangeKind.YESTERDAY:
        start = _local_midnight(reference_datetime, tz, day_offset=-1)
        end = start + timedelta(days=1)
    elif kind == DateRangeKind.THIS_WEEK:
        local = reference_datetime.astimezone(tz)
        start = _local_midnight(reference_datetime, tz, day_offset=-local.weekday())
        end = start + timedelta(days=7)
    elif kind == DateRangeKind.NEXT_WEEK:
        local = reference_datetime.astimezone(tz)
        start = _local_midnight(reference_datetime, tz, day_offset=-local.weekday() + 7)
        end = start + timedelta(days=7)
    elif kind == DateRangeKind.LAST_WEEK:
        local = reference_datetime.astimezone(tz)
        start = _local_midnight(reference_datetime, tz, day_offset=-local.weekday() - 7)
        end = start + timedelta(days=7)
    elif kind == DateRangeKind.THIS_MONTH:
        local = reference_datetime.astimezone(tz)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = _add_months(start, 1)
    elif kind == DateRangeKind.NEXT_MONTH:
        local = reference_datetime.astimezone(tz)
        this_month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = _add_months(this_month_start, 1)
        end = _add_months(this_month_start, 2)
    elif kind == DateRangeKind.BETWEEN:
        if explicit_start is None or explicit_end is None:
            raise ValueError("DateRangeKind.BETWEEN requires both explicit_start and explicit_end")
        start, end = explicit_start, explicit_end
    elif kind == DateRangeKind.BEFORE:
        if explicit_end is None:
            raise ValueError("DateRangeKind.BEFORE requires explicit_end")
        start, end = None, explicit_end
    elif kind == DateRangeKind.AFTER:
        if explicit_start is None:
            raise ValueError("DateRangeKind.AFTER requires explicit_start")
        start, end = explicit_start, None
    elif kind == DateRangeKind.UPCOMING:
        start, end = reference_datetime, None
    elif kind == DateRangeKind.OVERDUE:
        start, end = None, reference_datetime
    elif kind == DateRangeKind.RECENT:
        start, end = reference_datetime - timedelta(days=_RECENT_WINDOW_DAYS), reference_datetime
    elif kind == DateRangeKind.LATEST:
        start, end = None, None
    elif kind == DateRangeKind.ALL_TIME:
        start, end = None, None
    else:  # pragma: no cover -- StrEnum exhausts all defined kinds above
        raise ValueError(f"unhandled DateRangeKind: {kind}")

    return DateRange(kind=kind, start=start, end=end, reference_datetime=reference_datetime, timezone=timezone)


def _add_months(dt: datetime, months: int) -> datetime:
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    return dt.replace(year=year, month=month, day=1)
