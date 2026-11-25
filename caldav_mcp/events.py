"""The event as the rest of the server sees it, and as the agent sees it.

``EventView`` is a normalised, fully populated read of one VEVENT. ``shape`` turns
it into the compact dictionary that goes out, with every piece of third-party text
passed through :mod:`caldav_mcp.sanitize`.

Conventions the agent can rely on, stated once in the server instructions:

* Timed values carry the server's UTC offset (``2026-11-26T10:00+01:00``).
* ``all_day`` events have **dates**, and ``end`` is the **last day, inclusive** -
  the same meaning a date-only ``end`` has in every request. (iCalendar stores
  an exclusive end; that detail is not leaked.)
* ``floating`` means the event has no zone of its own, so it was read as server
  local time.
* ``day`` is the weekday of ``start``, because models miscompute weekdays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

from caldav_mcp.sanitize import assess, sanitize_short_text, sanitize_text

#: Fields a caller may request beyond the default ones.
INCLUDE_FIELDS = (
    "description",
    "attendees",
    "organizer",
    "categories",
    "reminders",
    "rrule",
    "etag",
    "url",
)
_SECONDS_PER_MINUTE = 60


@dataclass
class Attendee:
    """One ATTENDEE, with the display name kept apart from the address."""

    email: str
    name: str = ""
    status: str = ""
    role: str = ""

    def search_text(self) -> str:
        """Return the text a free-text query is matched against."""
        return f"{self.name} {self.email}".strip()


@dataclass
class EventView:
    """A normalised read of one VEVENT (or one expanded occurrence of it)."""

    uid: str
    calendar: str
    summary: str
    start: datetime | date
    end: datetime | date
    all_day: bool
    floating: bool
    event_tz: str = ""
    location: str = ""
    description: str = ""
    categories: list[str] = field(default_factory=list)
    status: str = ""
    transparent: bool = False
    organizer: Attendee | None = None
    attendees: list[Attendee] = field(default_factory=list)
    reminders: list[int] = field(default_factory=list)
    rrule: str = ""
    recurrence_id: datetime | date | None = None
    recurring: bool = False
    etag: str = ""
    url: str = ""

    @property
    def cancelled(self) -> bool:
        """Whether the event is marked cancelled."""
        return self.status == "CANCELLED"

    def start_instant(self, tz: tzinfo) -> datetime:
        """Return the start as an aware datetime (all-day values begin at local midnight)."""
        return _instant(self.start, tz)

    def end_instant(self, tz: tzinfo) -> datetime:
        """Return the exclusive end as an aware datetime (all-day values end at local midnight)."""
        return _instant(self.end, tz)


def _instant(value: datetime | date, tz: tzinfo) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return datetime.combine(value, time.min, tzinfo=tz)


def _text(comp: Any, name: str) -> str:
    value = comp.get(name)
    return "" if value is None else str(value)


def _attendee(prop: Any) -> Attendee:
    params = getattr(prop, "params", {}) or {}
    address = str(prop)
    email = address[7:] if address.lower().startswith("mailto:") else address
    return Attendee(
        email=email.strip().lower(),
        name=str(params.get("CN", "")),
        status=str(params.get("PARTSTAT", "")).upper(),
        role=str(params.get("ROLE", "")).upper(),
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list | tuple) else [value]


def _categories(comp: Any) -> list[str]:
    found: list[str] = []
    for prop in _as_list(comp.get("categories")):
        cats = getattr(prop, "cats", None)
        found.extend(str(c) for c in (cats if cats is not None else [prop]))
    return found


def _reminders(comp: Any) -> list[int]:
    """Minutes before the start, for relative triggers (absolute ones are not expressible that way)."""
    minutes: list[int] = []
    for sub in comp.subcomponents:
        if getattr(sub, "name", "") != "VALARM":
            continue
        trigger = sub.get("trigger")
        delta = getattr(trigger, "dt", None)
        if isinstance(delta, timedelta):
            minutes.append(round(-delta.total_seconds() / _SECONDS_PER_MINUTE))
    return sorted(set(minutes))


def _zone_name(value: datetime) -> str:
    key = getattr(value.tzinfo, "key", None)
    if key:
        return str(key)
    return "UTC" if value.utcoffset() == timedelta(0) else ""


def view_from_component(comp: Any, *, calendar: str, tz: tzinfo, etag: str = "", url: str = "") -> EventView | None:
    """Build an :class:`EventView`, or ``None`` for a component without a usable start."""
    start = comp.start if comp.get("dtstart") is not None else None
    if start is None:
        return None
    end = comp.end
    all_day = not isinstance(start, datetime)
    floating = isinstance(start, datetime) and start.tzinfo is None
    event_tz = "" if all_day or floating else _zone_name(start)
    if all_day:
        # iCalendar's DTEND is exclusive; the agent sees the last day, inclusive.
        end = max(start, end - timedelta(days=1)) if isinstance(end, date) else start
    elif isinstance(start, datetime) and isinstance(end, datetime):
        end = max(end, start)
    recurrence_id = getattr(comp.get("recurrence-id"), "dt", None)
    organizer = comp.get("organizer")
    return EventView(
        uid=_text(comp, "uid"),
        calendar=calendar,
        summary=_text(comp, "summary"),
        start=start,
        end=end,
        all_day=all_day,
        floating=floating,
        event_tz=event_tz if event_tz and event_tz != getattr(tz, "key", "UTC") else "",
        location=_text(comp, "location"),
        description=_text(comp, "description"),
        categories=_categories(comp),
        status=_text(comp, "status").upper(),
        transparent=_text(comp, "transp").upper() == "TRANSPARENT",
        organizer=_attendee(organizer) if organizer is not None else None,
        attendees=[_attendee(a) for a in _as_list(comp.get("attendee"))],
        reminders=_reminders(comp),
        rrule=comp["rrule"].to_ical().decode() if comp.get("rrule") is not None else "",
        recurrence_id=recurrence_id,
        recurring=recurrence_id is not None or comp.get("rrule") is not None,
        etag=etag,
        url=url,
    )


def format_value(value: datetime | date, tz: tzinfo) -> str:
    """Format a start or end for the agent: a date, or a local date-time with its offset."""
    if isinstance(value, datetime):
        local = value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
        return local.isoformat(timespec="minutes" if local.second == 0 else "seconds")
    return value.isoformat()


def sort_key(view: EventView, tz: tzinfo) -> tuple[datetime, datetime, str]:
    """Order by start, then end, then title; all-day events sort first on their day."""
    return (view.start_instant(tz).astimezone(UTC), view.end_instant(tz).astimezone(UTC), view.summary.casefold())


def shape(view: EventView, tz: tzinfo, include: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Build the compact dictionary for one event.

    Always present: ``uid``, ``summary``, ``start``, ``end``, ``day``, ``calendar``. Present
    only when informative: ``all_day``, ``floating``, ``event_tz``, ``location``, ``status``
    (when not confirmed), ``recurring``/``recurrence_id``, ``flags``. Everything else is
    behind ``include``.
    """
    out: dict[str, Any] = {
        "uid": view.uid,
        "summary": sanitize_short_text(view.summary),
        "start": format_value(view.start, tz),
        "end": format_value(view.end, tz),
        "day": f"{view.start_instant(tz):%a}",
        "calendar": sanitize_short_text(view.calendar),
    }
    if view.all_day:
        out["all_day"] = True
    if view.floating:
        out["floating"] = True
    if view.event_tz:
        out["event_tz"] = view.event_tz
    if view.location:
        out["location"] = sanitize_short_text(view.location)
    if view.status and view.status != "CONFIRMED":
        out["status"] = view.status
    if view.transparent:
        out["free"] = True
    if view.recurring:
        out["recurring"] = True
    if view.recurrence_id is not None:
        out["recurrence_id"] = format_value(view.recurrence_id, tz)
    flags = flags_for(view)
    if flags:
        out["flags"] = flags
    _add_optional(out, view, include)
    return out


def _add_optional(out: dict[str, Any], view: EventView, include: frozenset[str]) -> None:
    if "description" in include and view.description:
        out["description"] = sanitize_text(view.description)
    if "attendees" in include and view.attendees:
        out["attendees"] = [_shape_attendee(a) for a in view.attendees]
    if "organizer" in include and view.organizer is not None:
        out["organizer"] = _shape_attendee(view.organizer)
    if "categories" in include and view.categories:
        out["categories"] = [sanitize_short_text(c) for c in view.categories]
    if "reminders" in include and view.reminders:
        out["reminders_minutes_before"] = view.reminders
    if "rrule" in include and view.rrule:
        out["rrule"] = view.rrule
    if "etag" in include and view.etag:
        out["etag"] = view.etag
    if "url" in include and view.url:
        out["url"] = view.url


def _shape_attendee(attendee: Attendee) -> dict[str, str]:
    shaped = {"email": sanitize_short_text(attendee.email)}
    if attendee.name:
        shaped["name"] = sanitize_short_text(attendee.name)
    if attendee.status:
        shaped["status"] = attendee.status
    return shaped


def flags_for(view: EventView) -> list[str]:
    """Return the injection shapes found anywhere in the event's third-party text."""
    names = " ".join(a.name for a in view.attendees)
    blob = "\n".join([view.summary, view.location, view.description, names])
    signals = assess(blob)
    return [
        *signals.get("suspicious", []),
        *(f"mixed-script:{sanitize_short_text(w, 40)}" for w in signals.get("script_mix", [])),
    ]
