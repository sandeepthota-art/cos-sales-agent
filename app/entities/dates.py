import re
from datetime import datetime, timedelta
from typing import Literal

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_WEEKDAY_PATTERN = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE
)
_TOMORROW_PATTERN = re.compile(r"\btomorrow\b", re.IGNORECASE)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_EXPLICIT_DATE_PATTERN = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_RELATIVE_DURATION_PATTERN = re.compile(
    r"\bin\s+(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+(day|days|week|weeks)\b",
    re.IGNORECASE,
)
# Hour/minute-granularity relative timing (e.g. "in 2 hours", "in 30 minutes") -- a
# separate pattern from _RELATIVE_DURATION_PATTERN (day/week only) rather than folding
# hour/minute into it, so existing day/week resolution is untouched by construction.
# "30 minutes" and other bare numerals aren't in _NUMBER_WORDS, hence \d+ is required
# here (not optional the way day/week's word-form is) -- reminders phrased with a
# number word ("a couple hours") aren't covered; only digit amounts are.
_RELATIVE_TIME_PATTERN = re.compile(
    r"\bin\s+(\d+)\s+(hour|hours|minute|minutes)\b", re.IGNORECASE
)
_VAGUE_WINDOW_PATTERN = re.compile(
    r"\bnext month\b|\bnext week\b|\bsometime\b|\bend of (?:the )?month\b", re.IGNORECASE
)
_DATE_PHRASE_PATTERNS = [
    _EXPLICIT_DATE_PATTERN,
    _TOMORROW_PATTERN,
    _WEEKDAY_PATTERN,
    _RELATIVE_TIME_PATTERN,
    _RELATIVE_DURATION_PATTERN,
    _VAGUE_WINDOW_PATTERN,
]


def _next_weekday(reference: datetime, weekday: int) -> datetime:
    days_ahead = (weekday - reference.weekday()) % 7
    days_ahead = days_ahead or 7
    return reference + timedelta(days=days_ahead)


def find_date_phrase(text: str) -> str | None:
    """Return the first recognized date-related substring in text, verbatim, or None.

    Used by MockLLMProvider so the phrase a mention carries and the phrase
    resolve_date_phrase later resolves come from the same pattern set (spec S5.1.1).
    """
    for pattern in _DATE_PHRASE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def resolve_date_phrase(
    phrase: str | None, reference_now: datetime
) -> tuple[datetime | None, str | None]:
    if not phrase:
        return None, None

    explicit_match = _EXPLICIT_DATE_PATTERN.search(phrase)
    if explicit_match:
        month = _MONTHS[explicit_match.group(1).lower()[:3]]
        day = int(explicit_match.group(2))
        year = reference_now.year
        try:
            candidate = reference_now.replace(
                year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0
            )
            # Compare dates, not full timestamps: an email sent on June 5th referencing
            # "June 5th" means today, not next year. Comparing candidate < reference_now
            # (full precision) would incorrectly roll same-day references forward a year,
            # since candidate is normalized to midnight and reference_now carries a real
            # time-of-day that is almost always later than midnight.
            if candidate.date() < reference_now.date():
                candidate = candidate.replace(year=year + 1)
        except ValueError:
            # day is not valid for month/year (e.g. "June 45th", "Feb 30") -- a date was
            # clearly mentioned but can't be pinned to a specific day, so fall back to the
            # same "window" semantic used for other unresolvable-but-present date phrases.
            return None, "window"
        return candidate, "stated"

    if _TOMORROW_PATTERN.search(phrase):
        return reference_now + timedelta(days=1), "inferred"

    weekday_match = _WEEKDAY_PATTERN.search(phrase)
    if weekday_match:
        weekday = _WEEKDAYS[weekday_match.group(1).lower()]
        return _next_weekday(reference_now, weekday), "inferred"

    time_match = _RELATIVE_TIME_PATTERN.search(phrase)
    if time_match:
        amount = int(time_match.group(1))
        unit = time_match.group(2).lower()
        delta = timedelta(hours=amount) if unit.startswith("hour") else timedelta(minutes=amount)
        return reference_now + delta, "inferred"

    duration_match = _RELATIVE_DURATION_PATTERN.search(phrase)
    if duration_match:
        amount_word = duration_match.group(1).lower()
        amount = int(amount_word) if amount_word.isdigit() else _NUMBER_WORDS[amount_word]
        unit = duration_match.group(2).lower()
        days = amount * 7 if unit.startswith("week") else amount
        return reference_now + timedelta(days=days), "inferred"

    return None, "window"


def _add_business_days(start: datetime, count: int) -> datetime:
    """Adds `count` business days (Mon-Fri) to `start`, skipping weekends entirely --
    used for BRD 6.4's "business days" follow-up timing rule. This codebase has no
    holiday calendar, so a public holiday is not skipped."""
    current = start
    added = 0
    while added < count:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


FollowUpAudience = Literal["internal", "client_fixed_date", "client_open_window", "his_own_question"]


def classify_follow_up_timing(
    commitment_class: str,
    date_type: str | None,
    committed_date: datetime | None,
    is_internal_counterparty: bool,
) -> tuple[FollowUpAudience | None, datetime | None, datetime | None]:
    """BRD 6.4's audience-based follow-up timing. Applies ONLY to "owed_to_me"
    commitments (someone else committed to him) -- the caller (app.pipeline) never
    calls this for "mine"/"theirs"/"recap". "his_own_question" (a question HE asked,
    awaiting someone else's reply) is a valid return value in principle, but nothing in
    the current extraction pipeline distinguishes an unanswered question from any other
    commitment, so this function never returns it today -- see the implementation
    report for this gap.

    Returns (audience, earliest_at, latest_at). Ranges the BRD states as a range
    ("one to two days", "two to three business days") are returned as an explicit
    (earliest, latest) pair rather than collapsed into one invented single point.
    Returns (audience, None, None) whenever no anchor date exists to compute from
    (open-window commitments have no committed_date at all, by design -- see
    resolve_date_phrase, which never invents one).
    """
    if commitment_class != "owed_to_me":
        return None, None, None

    if is_internal_counterparty:
        # BRD 6.4: "Internal -- Chase one to two days after the stated deadline."
        if committed_date is None:
            return "internal", None, None
        return "internal", committed_date + timedelta(days=1), committed_date + timedelta(days=2)

    if date_type == "window" or committed_date is None:
        # BRD 6.4: "Client, open window -- Nudge at the edge of the window, asking for
        # a time." No anchor exists for a window-type date, so no timestamp is
        # computed -- see resolve_date_phrase's own "never invent an exact date" rule.
        return "client_open_window", None, None

    # BRD 6.4: "Client, fixed date -- Wait two to three business days past the date."
    return "client_fixed_date", _add_business_days(committed_date, 2), _add_business_days(committed_date, 3)
