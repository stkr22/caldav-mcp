"""Editing events and their recurrences, as pure functions over icalendar objects.

A stored event is one *resource* holding a **master** VEVENT and zero or more
**overrides** (components sharing the UID, each with a RECURRENCE-ID naming the
occurrence it replaces). Every function here takes the whole ``Calendar`` and
keeps every component it does not mean to change, because the old
read-the-first-component-and-write-it-back approach silently destroyed overrides.

Scopes, and what each does to the resource:

* ``series`` - change the master. If its start moves, the RECURRENCE-IDs and
  EXDATEs of existing exceptions move with it, so they keep pointing at the
  occurrences they were about.
* ``instance`` - change (or create) the override for one occurrence; delete adds
  an EXDATE for it so it does not regenerate.
* ``this_and_future`` - end the old series just before the occurrence (UNTIL; a
  COUNT becomes the number of occurrences left) and start a **new series** (new
  UID) from it, carrying along the exceptions that lie in the future.

Wall-clock arithmetic is deliberate where a human thinks in wall-clock ("the same
time next week"); real elapsed time (via UTC) is used for durations.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

import recurring_ical_events
from dateutil.rrule import rrulestr
from icalendar import Alarm, Calendar, Event, vRecur

from caldav_mcp.constants import UID_DOMAIN
from caldav_mcp.errors import ToolInputError
from caldav_mcp.events import format_value
from caldav_mcp.inputs import check_span, is_all_day, midnight

SCOPES = ("series", "instance", "this_and_future")
CLEARABLE = ("location", "description", "categories", "reminders", "rrule")
_NEIGHBOURS = 5
_SECOND = timedelta(seconds=1)


@dataclass
class Edits:
    """What an update wants changed. ``None`` means "leave as is"."""

    summary: str | None = None
    location: str | None = None
    description: str | None = None
    categories: list[str] | None = None
    reminders: list[int] | None = None
    rrule: str | None = None
    start: date | datetime | None = None
    #: Exclusive end, already resolved from a requested ``end`` (see ``inputs.end_exclusive``).
    end: date | datetime | None = None
    duration_minutes: int | None = None
    clear: frozenset[str] = field(default_factory=frozenset)

    def touches_time(self) -> bool:
        """Whether the edit moves or resizes the event."""
        return self.start is not None or self.end is not None or self.duration_minutes is not None


# --- reading the structure of a resource -------------------------------------------------------------------------


def events_of(cal: Calendar) -> list[Event]:
    """Return the VEVENTs of a resource."""
    return [c for c in cal.subcomponents if isinstance(c, Event)]


def split(cal: Calendar) -> tuple[Event, list[Event]]:
    """Return the master and the overrides; a resource without a master is refused."""
    comps = events_of(cal)
    masters = [c for c in comps if c.get("recurrence-id") is None]
    if not masters:
        raise ToolInputError("That event consists only of modified occurrences, with no series to change.")
    return masters[0], [c for c in comps if c.get("recurrence-id") is not None]


def is_recurring(master: Event, overrides: list[Event]) -> bool:
    """Whether the resource repeats (a rule, extra dates, or stored exceptions)."""
    return bool(master.get("rrule") is not None or master.get("rdate") is not None or overrides)


def instant(value: date | datetime, tz: tzinfo) -> datetime:
    """Return *value* as an aware UTC datetime for comparison (a date is local midnight)."""
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=tz) if value.tzinfo is None else value
        return aware.astimezone(UTC)
    return midnight(value, tz).astimezone(UTC)


def rule_of(comp: Event) -> vRecur | None:
    """Return the component's RRULE, if it has one (icalendar types property values too loosely to index)."""
    value = comp.get("rrule")
    return value if isinstance(value, vRecur) else None


def rule_text(comp: Event) -> str:
    """Return the RRULE as text, or an empty string."""
    rule = rule_of(comp)
    return rule.to_ical().decode() if rule is not None else ""


def _rid(comp: Event) -> date | datetime:
    value: Any = comp["recurrence-id"]
    return value.dt  # type: ignore[no-any-return]


def occurrence_ids(cal: Calendar, around: date | datetime, tz: tzinfo, span_days: int = 400) -> list[date | datetime]:
    """Return the RECURRENCE-IDs of expanded occurrences near *around*, in order."""
    centre = instant(around, tz)
    lo, hi = centre - timedelta(days=span_days), centre + timedelta(days=span_days)
    found = recurring_ical_events.of(cal).between(lo, hi)
    return sorted((_rid(c) for c in found if c.get("recurrence-id") is not None), key=lambda v: instant(v, tz))


def resolve_instance(cal: Calendar, requested: date | datetime, tz: tzinfo) -> date | datetime:
    """Map what the caller said to the RECURRENCE-ID of exactly one real occurrence, or explain why not.

    A full date-time must name an occurrence's original start. A plain date is accepted when exactly one
    occurrence begins on that local day.
    """
    master, _ = split(cal)
    ids = occurrence_ids(cal, requested, tz)
    if is_all_day(requested) and isinstance(master.start, datetime):
        same_day = [i for i in ids if instant(i, tz).astimezone(tz).date() == requested]
        if len(same_day) == 1:
            return same_day[0]
        if len(same_day) > 1:
            listed = ", ".join(format_value(i, tz) for i in same_day)
            raise ToolInputError(f"{len(same_day)} occurrences start on {requested}; give the exact one: {listed}.")
    else:
        wanted = instant(requested, tz)
        for candidate in ids:
            if instant(candidate, tz) == wanted:
                return candidate
    nearest = sorted(ids, key=lambda v: abs(instant(v, tz) - instant(requested, tz)))[:_NEIGHBOURS]
    listed = ", ".join(format_value(i, tz) for i in sorted(nearest, key=lambda v: instant(v, tz))) or "none"
    raise ToolInputError(
        f"No occurrence of this event starts at {format_value(requested, tz)}. `instance` is an occurrence's "
        f"original start (its `recurrence_id` in caldav_get_events). Nearest occurrences: {listed}."
    )


def find_override(overrides: list[Event], rid: date | datetime, tz: tzinfo) -> Event | None:
    """Return the stored exception for the occurrence *rid*, if there is one."""
    return next((o for o in overrides if instant(_rid(o), tz) == instant(rid, tz)), None)


def occurrence_component(cal: Calendar, rid: date | datetime, tz: tzinfo) -> Event:
    """Return the VEVENT that describes occurrence *rid* as it actually is.

    The stored exception if the occurrence was changed, otherwise a copy of the master moved to
    *rid* with the master's length - never the master's own start and end, which belong to the first
    occurrence.
    """
    master, overrides = split(cal)
    found = find_override(overrides, rid, tz)
    if found is not None:
        return found
    occurrence = copy.deepcopy(master)
    for name in ("rrule", "rdate", "exrule", "exdate", "dtstart", "dtend", "duration"):
        occurrence.pop(name, None)
    occurrence.add("dtstart", rid)
    occurrence.add("dtend", _shifted(rid, _elapsed(master.start, master.end), tz))
    occurrence.add("recurrence-id", rid)
    return occurrence


# --- applying edits to one component -----------------------------------------------------------------------------


def _elapsed(start: date | datetime, end: date | datetime) -> timedelta:
    if isinstance(start, datetime) and isinstance(end, datetime):
        return instant(end, UTC) - instant(start, UTC)
    return end - start  # type: ignore[operator]


def _shifted(start: date | datetime, length: timedelta, tz: tzinfo) -> date | datetime:
    if isinstance(start, datetime):
        aware = start if start.tzinfo else start.replace(tzinfo=tz)
        return (aware.astimezone(UTC) + length).astimezone(aware.tzinfo)
    return start + length


def _set(comp: Event, name: str, value: Any) -> None:
    comp.pop(name, None)
    comp.add(name, value)


def _replace_alarms(comp: Event, minutes: list[int]) -> None:
    comp.subcomponents[:] = [c for c in comp.subcomponents if getattr(c, "name", "") != "VALARM"]
    for before in minutes:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", "Reminder")
        alarm.add("trigger", timedelta(minutes=-before))
        comp.add_component(alarm)


def _new_times(comp: Event, edits: Edits, tz: tzinfo) -> tuple[date | datetime, date | datetime]:
    old_start, old_end = comp.start, comp.end
    start = edits.start if edits.start is not None else old_start
    if edits.end is not None:
        end = edits.end
    elif edits.duration_minutes is not None:
        if is_all_day(start):
            raise ToolInputError("An all-day event has no duration; give a date-only `end` for its last day.")
        end = _shifted(start, timedelta(minutes=edits.duration_minutes), tz)
    elif is_all_day(start) == is_all_day(old_start):
        end = _shifted(start, _elapsed(old_start, old_end), tz)  # moving keeps the length
    else:
        end = start + (timedelta(days=1) if is_all_day(start) else timedelta(hours=1))  # type: ignore[operator]
    check_span(start, end)
    return start, end


def apply_edits(comp: Event, edits: Edits, tz: tzinfo, now: datetime) -> None:
    """Apply *edits* to one VEVENT in place."""
    if edits.touches_time():
        start, end = _new_times(comp, edits, tz)
        for name in ("dtstart", "dtend", "duration"):
            comp.pop(name, None)
        comp.add("dtstart", start)
        comp.add("dtend", end)
    for name in ("summary", "location", "description"):
        value = getattr(edits, name)
        if value is not None:
            _set(comp, name, value)
    if edits.categories is not None:
        _set(comp, "categories", edits.categories)
    if edits.reminders is not None:
        _replace_alarms(comp, edits.reminders)
    if edits.rrule is not None:
        _set(comp, "rrule", vRecur.from_ical(edits.rrule))
    for name in edits.clear:
        if name == "reminders":
            _replace_alarms(comp, [])
        else:
            comp.pop(name, None)
    _touch(comp, now)


def _touch(comp: Event, now: datetime) -> None:
    stamp = now.astimezone(UTC)
    _set(comp, "dtstamp", stamp)
    _set(comp, "last-modified", stamp)
    if comp.get("sequence") is not None:
        _set(comp, "sequence", int(str(comp["sequence"])) + 1)


# --- moving the references to occurrences ------------------------------------------------------------------------


def _wall_shift(value: date | datetime, delta: timedelta) -> date | datetime:
    if isinstance(value, datetime):
        return (value.replace(tzinfo=None) + delta).replace(tzinfo=value.tzinfo)
    return value + timedelta(days=delta.days)


def _wall_delta(old: date | datetime, new: date | datetime) -> timedelta:
    if isinstance(old, datetime) and isinstance(new, datetime):
        return new.replace(tzinfo=None) - old.replace(tzinfo=None)
    if isinstance(old, datetime) or isinstance(new, datetime):
        return timedelta(0)
    return new - old


def _exdates(comp: Event) -> list[date | datetime]:
    props = comp.get("exdate")
    props = props if isinstance(props, list) else [props] if props is not None else []
    return [d.dt for prop in props for d in prop.dts]


def _set_exdates(comp: Event, values: list[date | datetime]) -> None:
    comp.pop("exdate", None)
    for value in values:
        comp.add("exdate", value)


# --- scopes ------------------------------------------------------------------------------------------------------


def edit_series(cal: Calendar, edits: Edits, tz: tzinfo, now: datetime) -> None:
    """Change the whole series, keeping its exceptions pointed at the right occurrences."""
    master, overrides = split(cal)
    old_start = master.start
    apply_edits(master, edits, tz, now)
    if edits.start is not None and (overrides or master.get("exdate") is not None):
        delta = _wall_delta(old_start, master.start)
        for override in overrides:
            _set(override, "recurrence-id", _wall_shift(_rid(override), delta))
        _set_exdates(master, [_wall_shift(d, delta) for d in _exdates(master)])


def edit_instance(cal: Calendar, rid: date | datetime, edits: Edits, tz: tzinfo, now: datetime) -> None:
    """Change one occurrence by editing or creating its override."""
    if edits.rrule is not None or "rrule" in edits.clear:
        raise ToolInputError(
            "A single occurrence cannot have its own recurrence rule; use scope 'series' or 'this_and_future'."
        )
    master, overrides = split(cal)
    override = find_override(overrides, rid, tz)
    if override is None:
        override = copy.deepcopy(master)
        for name in ("rrule", "rdate", "exrule", "exdate"):
            override.pop(name, None)
        _set(override, "recurrence-id", rid)
        for name in ("dtstart", "dtend", "duration"):
            override.pop(name, None)
        override.add("dtstart", rid)  # an unmodified occurrence starts exactly at its recurrence-id
        override.add("dtend", _shifted(rid, _elapsed(master.start, master.end), tz))
        cal.add_component(override)
    apply_edits(override, edits, tz, now)


def exclude_instance(cal: Calendar, rid: date | datetime, tz: tzinfo) -> None:
    """Cancel one occurrence: drop its override, if any, and add an EXDATE so it does not regenerate."""
    master, overrides = split(cal)
    override = find_override(overrides, rid, tz)
    if override is not None:
        cal.subcomponents.remove(override)
    if instant(rid, tz) not in {instant(d, tz) for d in _exdates(master)}:
        master.add("exdate", rid)


def _occurrences_before(master: Event, rid: date | datetime) -> int:
    start = master.start
    anchor = start if isinstance(start, datetime) else datetime.combine(start, datetime.min.time())
    rule = rrulestr(f"RRULE:{rule_text(master)}", dtstart=anchor)
    limit = rid if isinstance(rid, datetime) else datetime.combine(rid, datetime.min.time())
    if limit.tzinfo is None and anchor.tzinfo is not None:
        limit = limit.replace(tzinfo=anchor.tzinfo)
    return len(rule.between(anchor - _SECOND, limit, inc=False))


def _until_before(master: Event, rid: date | datetime, tz: tzinfo) -> date | datetime:
    start = master.start
    if not isinstance(start, datetime):
        return (rid.date() if isinstance(rid, datetime) else rid) - timedelta(days=1)
    if start.tzinfo is None:
        return (rid if isinstance(rid, datetime) else datetime.combine(rid, datetime.min.time())).replace(
            tzinfo=None
        ) - _SECOND
    return instant(rid, tz) - _SECOND


def truncate_before(
    cal: Calendar, rid: date | datetime, tz: tzinfo
) -> tuple[int | None, list[Event], list[date | datetime]]:
    """End the series just before *rid*.

    Returns the original COUNT-remaining basis ``(occurrences_before or None)``, the overrides that now
    belong to the future (removed here), and the EXDATEs that now belong to the future (removed here).
    """
    master, overrides = split(cal)
    rule = rule_of(master)
    if rule is None:
        raise ToolInputError("This event does not repeat, so there is no future to split off. Use scope 'series'.")
    before = _occurrences_before(master, rid) if "COUNT" in rule else None
    when = instant(rid, tz)
    future_overrides = [o for o in overrides if instant(_rid(o), tz) >= when]
    for override in future_overrides:
        cal.subcomponents.remove(override)
    exdates = _exdates(master)
    future_exdates = [d for d in exdates if instant(d, tz) >= when]
    _set_exdates(master, [d for d in exdates if instant(d, tz) < when])
    new_rule = vRecur(dict(rule))
    new_rule.pop("COUNT", None)
    new_rule["UNTIL"] = [_until_before(master, rid, tz)]
    _set(master, "rrule", new_rule)
    return before, future_overrides, future_exdates


def split_future(cal: Calendar, rid: date | datetime, edits: Edits, tz: tzinfo, now: datetime) -> Calendar:
    """Truncate the series before *rid* and return a new resource holding the series from *rid* onwards."""
    master, _ = split(cal)
    template = copy.deepcopy(master)
    rule = rule_of(master)
    if rule is None:
        raise ToolInputError("This event does not repeat, so there is no future to split off. Use scope 'series'.")
    total = int(rule["COUNT"][0]) if "COUNT" in rule else None
    before, future_overrides, future_exdates = truncate_before(cal, rid, tz)
    new_uid = f"{uuid.uuid4()}@{UID_DOMAIN}"
    length = _elapsed(master.start, master.end)
    first = rid
    fresh = Calendar()
    fresh.add("prodid", "-//caldav-mcp//EN")
    fresh.add("version", "2.0")
    head = copy.deepcopy(template)
    _set(head, "uid", new_uid)
    for name in ("dtstart", "dtend", "duration", "exdate"):
        head.pop(name, None)
    head.add("dtstart", first)
    head.add("dtend", _shifted(first, length, tz))
    new_rule = vRecur(dict(rule))
    new_rule.pop("UNTIL", None)
    if total is not None and before is not None:
        new_rule["COUNT"] = [max(1, total - before)]
    _set(head, "rrule", new_rule)
    for value in future_exdates:
        head.add("exdate", value)
    fresh.add_component(head)
    for override in future_overrides:
        moved = copy.deepcopy(override)
        _set(moved, "uid", new_uid)
        fresh.add_component(moved)
    apply_edits(head, edits, tz, now)
    if edits.start is not None:
        shift = _wall_delta(first, head.start)
        for moved in events_of(fresh):
            if moved is not head:
                _set(moved, "recurrence-id", _wall_shift(_rid(moved), shift))
        _set_exdates(head, [_wall_shift(d, shift) for d in _exdates(head)])
    _touch(master, now)
    return fresh
