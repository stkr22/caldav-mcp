"""Write operations: create, update, delete, move, and attendee changes.

Principles, each tied to a way an agent's write goes wrong:

* **Read the whole resource, change what was asked, write the whole resource.** Nothing the
  request did not mention is lost (overrides, alarms, attendees, unknown properties).
* **Recurring events need an explicit scope.** There is no default; the error lists the choices.
* **Conditional writes.** The ETag from the read in this same call is sent as ``If-Match``, so a
  change made elsewhere in between is a refusal, not a silent overwrite. A caller-supplied ETag is
  compared first. A server that returns no ETags gets an unconditional write, and the answer says so.
* **Say what happened.** Every answer carries the event as stored afterwards (re-read), and any
  conflicts or fallbacks, instead of echoing the request.
* **Roll back what we started.** The two-step operations (split a series, move by copy) undo their
  first step if the second fails, and say if they could not.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from caldav.lib import error as dav
from icalendar import Calendar, Event, vCalAddress, vText

from caldav_mcp.constants import ICAL_VERSION, PRODID, UID_DOMAIN
from caldav_mcp.dates import Range
from caldav_mcp.engine import Fetched, fetch_events, find_by_uid
from caldav_mcp.errors import ConflictError, ToolInputError
from caldav_mcp.events import EventView, format_value, shape, view_from_component
from caldav_mcp.filters import fold
from caldav_mcp.inputs import When, check_uid, email_field, midnight, parse_value
from caldav_mcp.series import (
    SCOPES,
    Edits,
    apply_edits,
    edit_instance,
    edit_series,
    exclude_instance,
    find_override,
    instant,
    is_recurring,
    occurrence_component,
    resolve_instance,
    rule_text,
    split,
    split_future,
    truncate_before,
)
from caldav_mcp.session import CalendarInfo
from caldav_mcp.tools.common import App

log = logging.getLogger("caldav-mcp")

_MOVE_FALLBACK_STATUSES = frozenset({400, 403, 405, 415, 501})
_MOVE_OK = frozenset({201, 204})
_MAX_CONFLICTS = 10


@dataclass
class Located:
    """An event found on the server, ready to be changed."""

    info: CalendarInfo
    resource: Any

    @property
    def etag(self) -> str:
        """The ETag as of this read, or empty when the server gives none."""
        return str(self.resource.etag or "")


def today_label(app: App) -> str:
    """Name today for error messages."""
    return f"{app.now():%A %Y-%m-%d}"


def locate(app: App, uid: str, calendar: str | None) -> Located:
    """Find the event, reading it fresh (and with its ETag) for this operation."""
    uid = check_uid(uid)
    info, resource = find_by_uid(app.session, app.session.resolve([calendar] if calendar else None), uid)
    return Located(info, resource)


def check_etag(found: Located, supplied: str | None) -> list[str]:
    """Compare a caller's ETag with the one just read; return the notes the answer must carry."""
    notes: list[str] = []
    current = found.etag
    if supplied and current and supplied.strip() != current:
        raise ConflictError(
            "The event changed on the server since you read it, so nothing was written. "
            "Read it again with caldav_get_event_by_uid and repeat the change if it still applies."
        )
    if not current:
        notes.append(
            "The server returned no ETag, so this write was unconditional: a change made elsewhere at the "
            "same moment could be overwritten." + (" The ETag you supplied could not be checked." if supplied else "")
        )
    return notes


def choose_scope(master: Event, overrides: list[Event], scope: str | None, instance: str | None, action: str) -> str:
    """Decide the scope for a change, refusing to guess for a recurring event."""
    recurring = is_recurring(master, overrides)
    if scope is not None and scope not in SCOPES:
        raise ToolInputError(f"scope must be one of: {', '.join(SCOPES)}.")
    if not recurring:
        if scope in ("instance", "this_and_future"):
            raise ToolInputError(
                "This event does not repeat, so scope 'instance' and 'this_and_future' do not apply; leave scope out."
            )
        return "series"
    if scope is None:
        rule = rule_text(master) or "extra dates"
        raise ToolInputError(
            f"This event repeats ({rule}), so say what to {action}: scope='series' (every occurrence), "
            "scope='instance' (one occurrence; pass `instance`, its recurrence_id from caldav_get_events), or "
            "scope='this_and_future' (that occurrence and every later one; pass `instance`)."
        )
    if scope != "series" and not instance:
        raise ToolInputError(
            f"scope '{scope}' needs `instance`: the occurrence's start, "
            "as `recurrence_id` shows it in caldav_get_events."
        )
    return scope


def resolve_rid(app: App, cal: Calendar, instance: str) -> date | datetime:
    """Turn the caller's ``instance`` into the RECURRENCE-ID of one real occurrence."""
    requested = parse_value(instance, app.config.tz, today_label(app), "instance")
    return resolve_instance(cal, requested, app.config.tz)


# --- create ------------------------------------------------------------------------------------------------------


def build_event(
    *,
    uid: str,
    summary: str,
    when: When,
    now: datetime,
    location: str | None,
    description: str | None,
    categories: list[str] | None,
    reminders: list[int] | None,
    rrule: str | None,
    tz: tzinfo,
) -> Calendar:
    """Build a new resource with the icalendar library (which escapes everything on serialisation)."""
    edits = Edits(location=location, description=description, categories=categories, reminders=reminders, rrule=rrule)
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", ICAL_VERSION)
    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", now.astimezone(UTC))
    event.add("summary", summary)
    event.add("dtstart", when.start)
    event.add("dtend", when.end)
    cal.add_component(event)
    apply_edits(event, edits, tz, now)
    cal.add_missing_timezones()
    return cal


def window_for(when: When, tz: tzinfo, tz_name: str) -> Range:
    """Return the range an event occupies, for conflict and duplicate checks."""
    if isinstance(when.start, datetime) and isinstance(when.end, datetime):
        return Range(when.start.astimezone(tz), when.end.astimezone(tz), tz_name)
    return Range(midnight(when.start, tz), midnight(when.end, tz), tz_name)  # type: ignore[arg-type]


def conflicts_for(app: App, when: When, exclude_uid: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Return timed events that overlap the new one in any calendar, and notes about the check."""
    cfg = app.config
    if when.all_day:
        return [], ["Conflict checks apply to timed events; this is an all-day event."]
    try:
        found = fetch_events(
            app.session, app.session.resolve(None), window_for(when, cfg.tz, cfg.tz_name), None, cfg.tz
        )
    except Exception:
        log.warning("conflict check failed", exc_info=True)
        return [], ["The conflict check could not run; the event was created anyway."]
    clashes = [v for v in found.views if v.uid != exclude_uid and not v.all_day and not v.transparent]
    shaped = [shape(v, cfg.tz) for v in clashes[:_MAX_CONFLICTS]]
    return shaped, ([f"Overlaps {len(clashes)} existing event(s); see `conflicts`."] if clashes else [])


def find_duplicate(found: Fetched, summary: str, when: When, tz: tzinfo) -> EventView | None:
    """Return an existing one-off event with the same title and times (the target calendar only was searched)."""
    for view in found.views:
        if view.recurring or fold(view.summary) != fold(summary):
            continue
        # A requested end is exclusive; an all-day view stores its last day, inclusive.
        last = when.end - timedelta(days=1) if view.all_day and not isinstance(when.end, datetime) else when.end  # type: ignore[operator]
        if view.start_instant(tz) == _as_instant(when.start, tz) and view.end_instant(tz) == _as_instant(last, tz):
            return view
    return None


def _as_instant(value: date | datetime, tz: tzinfo) -> datetime:
    return value.astimezone(tz) if isinstance(value, datetime) else midnight(value, tz)


def create(
    app: App, *, info: CalendarInfo, summary: str, when: When, extras: dict[str, Any], check_conflicts: bool
) -> dict[str, Any]:
    """Create the event, unless an identical one is already there; return what is stored."""
    cfg = app.config
    uid = f"{uuid.uuid4()}@{UID_DOMAIN}"
    now = app.now()
    cal = build_event(uid=uid, summary=summary, when=when, now=now, tz=cfg.tz, **extras)
    notes: list[str] = []
    conflicts: list[dict[str, Any]] = []
    if check_conflicts:
        window = window_for(when, cfg.tz, cfg.tz_name)
        probe = fetch_events(app.session, [info], window, None, cfg.tz)
        duplicate = find_duplicate(probe, summary, when, cfg.tz)
        if duplicate is not None:
            return {
                "created": False,
                "reason": "An identical event (same title, start and end) already exists in this calendar; "
                "nothing was created.",
                "event": shape(duplicate, cfg.tz),
            }
        conflicts, notes = conflicts_for(app, when, uid)
    with app.session.operation():
        info.handle.save_event(cal.to_ical().decode("utf-8"), no_overwrite=True)
    stored = locate(app, uid, info.id)
    view = view_from_component(
        split(stored.resource.icalendar_instance)[0], calendar=info.name, tz=cfg.tz, etag=stored.etag
    )
    payload: dict[str, Any] = {
        "created": True,
        "event": shape(view, cfg.tz, frozenset({"etag"})) if view else {"uid": uid},
    }
    if conflicts:
        payload["conflicts"] = conflicts
    if notes:
        payload["notes"] = notes
    return payload


# --- update ------------------------------------------------------------------------------------------------------


def update(
    app: App, found: Located, *, scope: str | None, instance: str | None, etag: str | None, edits: Edits
) -> dict[str, Any]:
    """Apply *edits* at the requested scope and return the event as stored afterwards."""
    cfg = app.config
    notes = check_etag(found, etag)
    with found.resource.edit_icalendar_instance() as cal:
        master, overrides = split(cal)
        chosen = choose_scope(master, overrides, scope, instance, "change")
        rid = resolve_rid(app, cal, instance) if chosen != "series" and instance else None
        if chosen == "this_and_future" and rid is not None and _is_first(master, rid, cfg.tz):
            chosen = "series"
            notes.append("That is the first occurrence, so the whole series was changed.")
        new_resource = None
        if chosen == "series":
            edit_series(cal, edits, cfg.tz, app.now())
        elif chosen == "instance" and rid is not None:
            edit_instance(cal, rid, edits, cfg.tz, app.now())
        elif rid is not None:
            fresh = split_future(cal, rid, edits, cfg.tz, app.now())
            fresh.add_missing_timezones()
            new_resource = fresh
        cal.add_missing_timezones()
    affected_uid = str(master["uid"])
    if new_resource is not None:
        affected_uid = _save_split(app, found, new_resource)
    else:
        found.resource.save(increase_seqno=False)
    stored = locate(app, affected_uid, found.info.id)
    return {
        "updated": True,
        "scope": chosen,
        **_describe(app, stored, rid if chosen == "instance" else None),
        "notes": notes,
    }


def _is_first(master: Event, rid: date | datetime, tz: tzinfo) -> bool:
    return instant(master.start, tz) == instant(rid, tz)


def _save_split(app: App, found: Located, fresh: Calendar) -> str:
    """Create the new series first, then shorten the old one; undo the first if the second fails."""
    new_uid = str(split(fresh)[0]["uid"])
    with app.session.operation():
        found.info.handle.save_event(fresh.to_ical().decode("utf-8"), no_overwrite=True)
    try:
        found.resource.save(increase_seqno=False)
    except Exception:
        try:
            created = found.info.handle.get_event_by_uid(new_uid)
            created.delete()
            rolled_back = "The new series was removed again, so nothing changed."
        except Exception:
            rolled_back = f"The new series (uid {new_uid}) could NOT be removed; delete it to avoid duplicates."
        log.warning("split failed after creating the new series; %s", rolled_back, exc_info=True)
        raise ConflictError(
            f"Shortening the original series failed after the new one was created. {rolled_back}"
        ) from None
    return new_uid


def _describe(app: App, stored: Located, rid: date | datetime | None) -> dict[str, Any]:
    cfg = app.config
    cal = stored.resource.icalendar_instance
    master, overrides = split(cal)
    comp = master
    if rid is not None:
        comp = find_override(overrides, rid, cfg.tz) or master
    view = view_from_component(comp, calendar=stored.info.name, tz=cfg.tz, etag=stored.etag)
    return {"event": shape(view, cfg.tz, frozenset({"etag"})) if view else {"uid": str(master["uid"])}}


# --- delete ------------------------------------------------------------------------------------------------------


def delete(app: App, found: Located, *, scope: str | None, instance: str | None, etag: str | None) -> dict[str, Any]:
    """Delete the event, one occurrence, or an occurrence and everything after it."""
    cfg = app.config
    notes = check_etag(found, etag)
    cal = found.resource.icalendar_instance
    master, overrides = split(cal)
    chosen = choose_scope(master, overrides, scope, instance, "delete")
    rid = resolve_rid(app, cal, instance) if chosen != "series" and instance else None
    if chosen == "this_and_future" and rid is not None and _is_first(master, rid, cfg.tz):
        chosen = "series"
        notes.append("That is the first occurrence, so the whole series was deleted.")
    described = occurrence_component(cal, rid, cfg.tz) if chosen == "instance" and rid is not None else master
    view = view_from_component(described, calendar=found.info.name, tz=cfg.tz)
    removed = shape(view, cfg.tz) if view else {"uid": str(master["uid"])}
    if chosen == "series":
        _delete_resource(found)
    else:
        with found.resource.edit_icalendar_instance() as editable:
            if chosen == "instance" and rid is not None:
                exclude_instance(editable, rid, cfg.tz)
            elif rid is not None:
                truncate_before(editable, rid, cfg.tz)
        found.resource.save(increase_seqno=False)
        if chosen == "this_and_future" and rid is not None:
            removed = {**removed, "from_occurrence": format_value(rid, cfg.tz)}
    return {"deleted": True, "scope": chosen, "event": removed, "notes": notes}


def _delete_resource(found: Located) -> None:
    if found.etag:
        found.resource.delete(if_match=True)
    else:
        found.resource.delete()


# --- move --------------------------------------------------------------------------------------------------------


def move(app: App, found: Located, target: CalendarInfo) -> dict[str, Any]:
    """Move the event to *target*, keeping its UID; WebDAV MOVE where the server has it, else copy and delete."""
    if found.info.id == target.id:
        raise ToolInputError("The event is already in that calendar.")
    uid = str(split(found.resource.icalendar_instance)[0]["uid"])
    with app.session.operation() as client:
        try:
            target.handle.get_event_by_uid(uid)
        except dav.NotFoundError:
            pass
        else:
            raise ConflictError("An event with this uid already exists in the target calendar; nothing was moved.")
        filename = str(found.resource.url).rstrip("/").rsplit("/", 1)[-1]
        destination = f"{target.url.rstrip('/')}/{filename}"
        response = client.request(
            str(found.resource.url), "MOVE", headers={"Destination": destination, "Overwrite": "F"}
        )
        if response.status in _MOVE_OK:
            method = "MOVE"
        elif response.status == 412:  # noqa: PLR2004
            raise ConflictError("The destination already holds an event with that name; nothing was moved.")
        elif response.status in _MOVE_FALLBACK_STATUSES:
            method = _copy_then_delete(found, target)
        else:
            raise dav.DAVError(url=str(found.resource.url), reason=f"MOVE answered {response.status}")
    app.session.invalidate()
    moved = locate(app, uid, target.id)
    return {
        "moved": True,
        "from": found.info.label(),
        "to": target.label(),
        "method": method,
        **_describe(app, moved, None),
    }


def _copy_then_delete(found: Located, target: CalendarInfo) -> str:
    raw = found.resource.raw_data or found.resource.data
    target.handle.save_event(
        raw.decode("utf-8") if isinstance(raw, bytes | bytearray | memoryview) else raw, no_overwrite=True
    )
    try:
        _delete_resource(found)
    except Exception:
        try:
            uid = str(split(found.resource.icalendar_instance)[0]["uid"])
            target.handle.get_event_by_uid(uid).delete()
            undone = "The copy was removed again, so nothing changed."
        except Exception:
            undone = "The copy in the target could NOT be removed; the event now exists in both calendars."
        raise ConflictError(f"Deleting the original failed after copying. {undone}") from None
    return "copy then delete (the server has no MOVE)"


# --- attendees ---------------------------------------------------------------------------------------------------


def change_attendee(
    app: App, found: Located, *, email: str, add: bool, name: str | None, role: str, scope: str | None, etag: str | None
) -> dict[str, Any]:
    """Add or remove one attendee on the whole event (all occurrences of a series)."""
    cfg = app.config
    address = email_field(email)
    notes = check_etag(found, etag)
    with found.resource.edit_icalendar_instance() as cal:
        master, overrides = split(cal)
        if is_recurring(master, overrides) and scope != "series":
            raise ToolInputError(
                "This event repeats. Attendees apply to the whole series; pass scope='series' to confirm."
            )
        current = master.get("attendee")
        current = current if isinstance(current, list) else [current] if current is not None else []
        keep = [a for a in current if str(a).lower().removeprefix("mailto:") != address]
        if add:
            if len(keep) != len(current):
                notes.append("That address was already an attendee; nothing changed.")
            else:
                person = vCalAddress(f"mailto:{address}")
                person.params["ROLE"] = vText(role)
                person.params["PARTSTAT"] = vText("NEEDS-ACTION")
                person.params["RSVP"] = vText("TRUE")
                if name:
                    person.params["CN"] = vText(name)
                master.add("attendee", person, encode=False)
        else:
            if len(keep) == len(current):
                listed = ", ".join(str(a).removeprefix("mailto:") for a in current) or "none"
                raise ToolInputError(f"{address} is not an attendee of this event. Current attendees: {listed}.")
            master.pop("attendee", None)
            for person in keep:
                master.add("attendee", person, encode=False)
        cal.add_missing_timezones()
    found.resource.save(increase_seqno=False)
    stored = locate(app, str(master["uid"]), found.info.id)
    shaped = _describe(app, stored, None)
    view = view_from_component(split(stored.resource.icalendar_instance)[0], calendar=stored.info.name, tz=cfg.tz)
    attendees = [{"email": a.email, "name": a.name, "status": a.status} for a in (view.attendees if view else [])]
    return {"added" if add else "removed": address, "attendees": attendees, "event": shaped["event"], "notes": notes}
