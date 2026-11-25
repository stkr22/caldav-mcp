"""Validation of what an agent asks to write.

Everything that ends up inside an iCalendar object passes through here first, and
the iCalendar objects themselves are built with the icalendar library (which
escapes on serialisation) - never by string concatenation. Values that are wrong
are refused with the correction, not repaired: a guessed time on a calendar is
worse than a question.

Writes accept **exact ISO values only** (``2026-11-28``, ``2026-11-28T15:00``).
The keywords reads understand ("tomorrow") would turn a missing time into an
all-day event, so they are refused, and the error says what today is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from icalendar import vRecur

from caldav_mcp.errors import ToolInputError
from caldav_mcp.sanitize import has_control_characters, quoted, strip_invisible

MAX_SUMMARY = 256
MAX_LOCATION = 512
MAX_DESCRIPTION = 4096
MAX_CATEGORY = 64
MAX_CATEGORIES = 10
MAX_UID = 255
MAX_REMINDERS = 5
MAX_REMINDER_MINUTES = 40_320  # four weeks
MAX_DURATION_DAYS = 366
DEFAULT_DURATION = timedelta(hours=1)

_EMAIL = re.compile(
    r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$"
)
_RRULE_KEYS = frozenset(
    {
        "FREQ",
        "INTERVAL",
        "COUNT",
        "UNTIL",
        "BYDAY",
        "BYMONTHDAY",
        "BYMONTH",
        "BYSETPOS",
        "BYYEARDAY",
        "BYWEEKNO",
        "WKST",
    }
)
_FREQUENCIES = frozenset({"DAILY", "WEEKLY", "MONTHLY", "YEARLY"})
MAX_RRULE_COUNT = 1000
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def text_field(
    value: str | None, name: str, max_length: int, *, multiline: bool = False, required: bool = False
) -> str | None:
    """Validate a free-text value. ``None`` means "not given"; an empty string is refused, not guessed at."""
    if value is None:
        if required:
            raise ToolInputError(f"{name} is required.")
        return None
    normalised = value.replace("\r\n", "\n").replace("\r", "\n")
    # Refuse controls before anything is stripped: removing them quietly would store text the caller did not write.
    if has_control_characters(normalised, allow_newlines=multiline) or (not multiline and "\n" in normalised):
        kind = "text" if multiline else "a single line of text"
        raise ToolInputError(f"{name} must be {kind} without control characters.")
    cleaned = strip_invisible(normalised).strip()
    if not cleaned:
        hint = "" if required else f", or list {name!r} in `clear` to remove it"
        raise ToolInputError(f"{name} is empty. Leave it out to keep the current value{hint}.")
    if len(cleaned) > max_length:
        raise ToolInputError(f"{name} is {len(cleaned)} characters; at most {max_length} are allowed.")
    return cleaned


def check_uid(uid: str) -> str:
    """Validate a uid taken from another tool's answer; it is never composed, so anything odd is refused."""
    cleaned = uid.strip()
    if not cleaned or len(cleaned) > MAX_UID or has_control_characters(cleaned):
        raise ToolInputError("uid must be the event's uid exactly as another tool returned it.")
    return cleaned


def categories_field(values: list[str] | None) -> list[str] | None:
    """Validate a list of category labels."""
    if values is None:
        return None
    if not values or len(values) > MAX_CATEGORIES:
        raise ToolInputError(f"categories needs 1 to {MAX_CATEGORIES} labels; use `clear` to remove them.")
    cleaned = []
    for value in values:
        label = text_field(value, "a category", MAX_CATEGORY, required=True)
        if label is None or "," in label:
            raise ToolInputError("Category labels must not contain commas.")
        cleaned.append(label)
    return cleaned


def reminders_field(values: list[int] | None) -> list[int] | None:
    """Validate reminder offsets, in minutes before the start."""
    if values is None:
        return None
    if not values or len(values) > MAX_REMINDERS:
        raise ToolInputError(f"reminders_minutes_before needs 1 to {MAX_REMINDERS} values; use `clear` to remove them.")
    if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_REMINDER_MINUTES for v in values):
        raise ToolInputError(f"Reminders are whole minutes before the start, between 0 and {MAX_REMINDER_MINUTES}.")
    return sorted(set(values))


def email_field(value: str) -> str:
    """Validate and normalise an attendee address (``mailto:`` is accepted and removed)."""
    address = value.strip()
    if address.lower().startswith("mailto:"):
        address = address[7:]
    if len(address) > 254 or not _EMAIL.match(address):  # noqa: PLR2004 - RFC 5321 address length
        raise ToolInputError(f"{quoted(value, 60)!r} is not a valid email address.")
    return address.lower()


def rrule_field(value: str | None) -> str | None:
    """Validate an RFC 5545 recurrence rule and return it normalised."""
    if value is None:
        return None
    raw = strip_invisible(value).strip()
    if raw.upper().startswith("RRULE:"):
        raw = raw[6:]
    try:
        rule = vRecur.from_ical(raw)
    except Exception:
        rule = None
    problem = _rrule_problem(rule)
    if problem or rule is None:
        raise ToolInputError(
            f"rrule {quoted(value, 80)!r} is not usable: {problem or 'not a valid rule'}. "
            "Example: FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10 (or UNTIL=20261231T000000Z)."
        )
    return rule.to_ical().decode()


def _rrule_problem(rule: vRecur | None) -> str:
    if not rule:
        return "it has no parts"
    keys = {str(k).upper() for k in rule}
    if keys - _RRULE_KEYS:
        return f"unsupported part(s) {', '.join(sorted(keys - _RRULE_KEYS))}"
    if str(rule.get("FREQ", [""])[0]).upper() not in _FREQUENCIES:
        return "FREQ must be DAILY, WEEKLY, MONTHLY or YEARLY"
    if "COUNT" in rule and "UNTIL" in rule:
        return "COUNT and UNTIL cannot both be given"
    count = rule.get("COUNT", [1])[0]
    if not 1 <= int(count) <= MAX_RRULE_COUNT:
        return f"COUNT must be between 1 and {MAX_RRULE_COUNT}"
    return ""


@dataclass(frozen=True)
class When:
    """A resolved start and end. ``end`` is exclusive (an all-day event's end is the day after its last day)."""

    start: date | datetime
    end: date | datetime
    all_day: bool


def parse_value(raw: str, tz: tzinfo, today: str, name: str) -> date | datetime:
    """Parse one exact ISO date or date-time; words such as "tomorrow" are refused with a pointer to today."""
    text = raw.strip()
    try:
        if _ISO_DATE.fullmatch(text):
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ToolInputError(
            f"{name} {quoted(raw, 60)!r} is not an exact ISO date (2026-11-28) or date-time (2026-11-28T15:00). "
            f"Words such as 'tomorrow' are not accepted for writes; work out the date from today ({today})."
        ) from None
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)


def is_all_day(value: date | datetime) -> bool:
    """Whether *value* is a plain date (an all-day start or end)."""
    return not isinstance(value, datetime)


def end_exclusive(last: date | datetime) -> date | datetime:
    """Return the exclusive end for a requested ``end`` (a date-only end is its last day, inclusive)."""
    return last + timedelta(days=1) if is_all_day(last) else last  # type: ignore[operator]


def check_span(start: date | datetime, end: date | datetime) -> None:
    """Refuse an end that is not after the start, or an event longer than a year."""
    if is_all_day(start) != is_all_day(end):
        raise ToolInputError("start and end must both be dates (all-day) or both be date-times.")
    if end <= start:  # type: ignore[operator]
        raise ToolInputError("end must be after start.")
    if (end - start) > timedelta(days=MAX_DURATION_DAYS):  # type: ignore[operator]
        raise ToolInputError(f"An event can last at most {MAX_DURATION_DAYS} days.")


def new_event_when(start: str, end: str | None, duration_minutes: int | None, *, tz: tzinfo, today: str) -> When:
    """Resolve a new event's times.

    A date-only start makes an all-day event (one day unless a date-only ``end`` gives its last day). A
    date-time start lasts until ``end``, or for ``duration_minutes``, or for one hour.
    """
    if not start or not start.strip():
        raise ToolInputError("start is required: an ISO date (all-day) or date-time.")
    first = parse_value(start, tz, today, "start")
    if end and duration_minutes is not None:
        raise ToolInputError("Give `end` or `duration_minutes`, not both.")
    if duration_minutes is not None and not 1 <= duration_minutes <= MAX_DURATION_DAYS * 1440:
        raise ToolInputError(f"duration_minutes must be between 1 and {MAX_DURATION_DAYS * 1440}.")
    if is_all_day(first):
        if duration_minutes is not None:
            raise ToolInputError("An all-day event has no duration; give a date-only `end` for its last day.")
        finish = end_exclusive(parse_value(end, tz, today, "end")) if end else first + timedelta(days=1)  # type: ignore[operator]
    elif end:
        finish = parse_value(end, tz, today, "end")
    else:
        finish = first + (timedelta(minutes=duration_minutes) if duration_minutes else DEFAULT_DURATION)  # type: ignore[operator]
    check_span(first, finish)
    return When(first, finish, is_all_day(first))


def midnight(day: date, tz: tzinfo) -> datetime:
    """Return local midnight of *day*."""
    return datetime.combine(day, time.min, tzinfo=tz)
