"""Turning what an agent says ("tomorrow", "2026-11-28", "last_month") into a time range.

Rules, each of which exists because the alternative produces a silent wrong
answer:

* Day and week boundaries are computed on **local calendar dates** and only then
  attached to the zone, so a day is 23 or 25 hours long across a DST change
  instead of drifting by an hour.
* A **date-only end is inclusive** ("Saturday to Sunday" covers Sunday). A
  date-time end is an exclusive instant. The resolved range is echoed in every
  answer, so a misreading is visible instead of looking like "nothing there".
* An end before the start, a zero-length range and an over-long range are
  errors that say how to fix the call, never an empty result.
* Weeks start on the configured day (Monday by default); a weekend is always
  Saturday and Sunday.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from caldav_mcp.errors import ToolInputError
from caldav_mcp.sanitize import quoted

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_SATURDAY = 5
_DAYS_IN_WEEK = 7
_MONTHS_IN_YEAR = 12
_MAX_N_DAYS = 366

KEYWORDS = (
    "now, today, tomorrow, yesterday, this_week, next_week, last_week, this_weekend, next_weekend, "
    "this_month, next_month, last_month, next_N_days, last_N_days, a weekday name (monday..sunday), "
    "next_<weekday>"
)


@dataclass(frozen=True)
class Range:
    """A resolved ``[start, end)`` interval in the server time zone."""

    start: datetime
    end: datetime
    tz_name: str

    @property
    def days(self) -> float:
        """Length in 24-hour days (wall-clock, which is what a range limit means)."""
        return (self.end - self.start).total_seconds() / 86400

    def describe(self) -> dict[str, str]:
        """Describe the range for an answer, with weekday names the model cannot get wrong."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "tz": self.tz_name,
            "label": f"{self.start:%a %Y-%m-%d %H:%M} to {self.end:%a %Y-%m-%d %H:%M} ({self.tz_name}), end exclusive",
        }


def _midnight(day: date, tz: tzinfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=tz)


def _first_of_month(day: date, offset: int) -> date:
    index = day.year * _MONTHS_IN_YEAR + (day.month - 1) + offset
    return date(index // _MONTHS_IN_YEAR, index % _MONTHS_IN_YEAR + 1, 1)


def _week_start_date(today: date, week_start: int) -> date:
    return today - timedelta(days=(today.weekday() - week_start) % _DAYS_IN_WEEK)


def _days(first: date, count: int) -> tuple[date, date]:
    return first, first + timedelta(days=count)


def _keyword_dates(token: str, today: date, week_start: int) -> tuple[date, date] | None:
    """Return the ``[first, last+1)`` date span a keyword stands for, or None if it is not one."""
    token = re.sub(r"[\s-]+", "_", token.strip().lower())
    this_week = _week_start_date(today, week_start)
    # Positive Monday-Friday, 0 on Saturday, -1 on Sunday: on a weekend, "this weekend" is the current one.
    saturday = today + timedelta(days=_SATURDAY - today.weekday())
    fixed = {
        "today": _days(today, 1),
        "tomorrow": _days(today + timedelta(days=1), 1),
        "yesterday": _days(today - timedelta(days=1), 1),
        "this_week": _days(this_week, _DAYS_IN_WEEK),
        "next_week": _days(this_week + timedelta(days=_DAYS_IN_WEEK), _DAYS_IN_WEEK),
        "last_week": _days(this_week - timedelta(days=_DAYS_IN_WEEK), _DAYS_IN_WEEK),
        "this_weekend": _days(saturday, 2),
        "next_weekend": _days(saturday + timedelta(days=_DAYS_IN_WEEK), 2),
        "this_month": (_first_of_month(today, 0), _first_of_month(today, 1)),
        "next_month": (_first_of_month(today, 1), _first_of_month(today, 2)),
        "last_month": (_first_of_month(today, -1), _first_of_month(today, 0)),
    }
    if token in fixed:
        return fixed[token]
    if token in _WEEKDAYS:  # today counts: "thursday" on a Thursday is today
        return _days(today + timedelta(days=(_WEEKDAYS.index(token) - today.weekday()) % _DAYS_IN_WEEK), 1)
    if token.startswith("next_") and token[5:] in _WEEKDAYS:  # strictly after today
        ahead = (_WEEKDAYS.index(token[5:]) - today.weekday()) % _DAYS_IN_WEEK or _DAYS_IN_WEEK
        return _days(today + timedelta(days=ahead), 1)
    counted = re.fullmatch(r"(next|last)_(\d{1,3})_days?", token)
    if counted:
        count = int(counted.group(2))
        if not 1 <= count <= _MAX_N_DAYS:
            raise ToolInputError(f"{counted.group(1)}_N_days needs N between 1 and {_MAX_N_DAYS}.")
        return _days(today, count) if counted.group(1) == "next" else _days(today - timedelta(days=count - 1), count)
    return None


@dataclass(frozen=True)
class _Bound:
    """One side of a range: an instant, or a whole span of days (date-only or keyword)."""

    start: datetime
    end: datetime
    is_span: bool


def _parse_bound(raw: str, now: datetime, tz: tzinfo, week_start: int) -> _Bound:
    text = raw.strip()
    if text.lower() == "now":
        return _Bound(now, now, is_span=False)
    span = _keyword_dates(text, now.astimezone(tz).date(), week_start)
    if span is not None:
        return _Bound(_midnight(span[0], tz), _midnight(span[1], tz), is_span=True)
    try:
        return _parse_iso(text, tz)
    except ValueError:
        raise ToolInputError(
            f"Could not read {quoted(raw, 60)!r} as a date or time. Use an ISO date (2026-11-28), an ISO "
            f"date-time (2026-11-28T15:00 or with an offset such as +01:00), or one of: {KEYWORDS}."
        ) from None


def _parse_iso(text: str, tz: tzinfo) -> _Bound:
    if re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", text):
        day = date.fromisoformat(text)
        return _Bound(_midnight(day, tz), _midnight(day + timedelta(days=1), tz), is_span=True)
    parsed = datetime.fromisoformat(text)
    aware = parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)
    return _Bound(aware, aware, is_span=False)


def resolve_range(
    start: str,
    end: str,
    *,
    now: datetime,
    tz: tzinfo,
    tz_name: str,
    week_start: int = 0,
    default_start: str = "today",
    default_span: timedelta | None = None,
    max_days: float = 366,
) -> Range:
    """Resolve *start* and *end* (either may be empty) into a :class:`Range`.

    With no *end*: a keyword or a date-only start covers its own span (``today``,
    ``2026-11-28``); a date-time start covers *default_span*, which tools set to
    what they mean by "at 15:00" (an hour for availability, a day for an agenda).
    """
    first = _parse_bound(start or default_start, now, tz, week_start)
    begin = first.start
    if end.strip():
        last = _parse_bound(end, now, tz, week_start)
        finish = last.end  # a span end is inclusive of its whole last day
    elif first.is_span:
        finish = first.end
    else:
        finish = begin + (default_span or timedelta(days=1))
    if finish < begin:
        raise ToolInputError(
            f"The end ({finish:%Y-%m-%d %H:%M}) is before the start ({begin:%Y-%m-%d %H:%M}). "
            "Swap them, or give only a start."
        )
    if finish == begin:
        raise ToolInputError(
            "The range is empty (start equals end). Give a later end, for example one hour after the start, "
            "or a date-only value to cover the whole day."
        )
    result = Range(begin, finish, tz_name)
    if result.days > max_days:
        raise ToolInputError(
            f"The range spans {result.days:.0f} days; at most {max_days:.0f} are allowed in one call. Narrow it."
        )
    return result
