"""Reading events: the time-range query, the server-side filter, the local re-check.

What runs where, and what the answer says about it:

* **Time range** - always the server (RFC 4791 ``calendar-query`` with a
  ``time-range``); a calendar with thousands of events is never downloaded whole.
* **Recurrence expansion** - python-caldav, client-side, for consistent results
  across servers (expansion by the server is optional in the RFC and differs in
  practice). Exceptions (RECURRENCE-ID), EXDATE, UNTIL and COUNT are honoured.
* **Text conditions** - pushed to the server as ``text-match`` only where the
  plan says the server will provably agree (see :mod:`caldav_mcp.filters`); every
  result is re-checked here, so the answer is exact regardless.

The returned :class:`Fetched` records which of these happened so the answer can
say so instead of leaving an empty list to be read as "nothing there".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta, tzinfo
from typing import Any

from caldav.lib import error as dav
from caldav.search import CalDAVSearcher

from caldav_mcp.dates import Range
from caldav_mcp.errors import EventNotFoundError, to_tool_error
from caldav_mcp.events import EventView, format_value, view_from_component
from caldav_mcp.filters import Node, ServerPlan, matches, plan_server_filter
from caldav_mcp.sanitize import sanitize_short_text
from caldav_mcp.session import CalendarInfo, Session

log = logging.getLogger("caldav-mcp")

_WIRE_NAME = {"summary": "SUMMARY", "location": "LOCATION", "description": "DESCRIPTION", "categories": "category"}


@dataclass
class Fetched:
    """Events found in a range, with an honest account of how."""

    views: list[EventView] = field(default_factory=list)
    #: Events the server returned for the range (and pushed-down text), before the local re-check.
    considered: int = 0
    cancelled_hidden: int = 0
    unreadable: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    plan: ServerPlan = field(default_factory=ServerPlan)
    calendars: list[str] = field(default_factory=list)

    @property
    def path(self) -> str:
        """Say where the text conditions were evaluated."""
        if not self.plan.terms and self.plan.exact:
            return "server (time range only)"
        if self.plan.terms and self.plan.exact:
            return "server (time range and text), re-checked locally"
        if self.plan.terms:
            return "server (time range and part of the text), finished locally"
        return "server (time range), text matched locally"


def build_searcher(rng: Range, plan: ServerPlan) -> CalDAVSearcher:
    """Build the calendar-query for *rng* with the pushed-down text conditions."""
    searcher = CalDAVSearcher(event=True, start=rng.start, end=rng.end, expand=True)
    for name, term in plan.terms:
        searcher.add_property_filter(_WIRE_NAME[name], term, operator="contains", case_sensitive=False)
    return searcher


def fetch_events(session: Session, calendars: list[CalendarInfo], rng: Range, node: Node | None, tz: tzinfo) -> Fetched:
    """Read the events of *calendars* in *rng* that satisfy *node*.

    A calendar that fails is reported in ``errors`` and the rest are still answered; only
    when every calendar fails is the failure raised, because then there is nothing to say.
    """
    plan = plan_server_filter(node)
    out = Fetched(plan=plan, calendars=[c.label() for c in calendars])
    searcher = build_searcher(rng, plan)
    failures: list[BaseException] = []
    with session.operation():
        for info in calendars:
            try:
                found = searcher.search(info.handle)
            except Exception as exc:
                failures.append(exc)
                out.errors.append({"calendar": info.label(), "error": str(to_tool_error(exc, "search"))})
                continue
            for event in found:
                _collect(out, event, info, node, tz)
    if failures and len(failures) == len(calendars):
        raise failures[0]
    return out


def _collect(out: Fetched, event: Any, info: CalendarInfo, node: Node | None, tz: tzinfo) -> None:
    view = view_from_component(event.icalendar_component, calendar=info.name, tz=tz, url=str(event.url or ""))
    if view is None:
        out.unreadable += 1
        return
    out.considered += 1
    if view.cancelled:
        out.cancelled_hidden += 1
        return
    if node is None or matches(view, node):
        out.views.append(view)


def find_by_uid(session: Session, calendars: list[CalendarInfo], uid: str) -> tuple[CalendarInfo, Any]:
    """Return the calendar and the (unexpanded) resource holding *uid*."""
    with session.operation():
        for info in calendars:
            try:
                return info, info.handle.get_event_by_uid(uid)
            except dav.NotFoundError:
                continue
    where = ", ".join(repr(c.label()) for c in calendars)
    raise EventNotFoundError(
        f"No event with that UID in {where}. Use caldav_search_events or caldav_get_events to find the right one."
    )


def series_detail(resource: Any, tz: tzinfo) -> dict[str, Any]:
    """Describe a recurring event's exceptions: dates removed, and instances moved or edited."""
    components = [c for c in resource.icalendar_instance.subcomponents if getattr(c, "name", "") == "VEVENT"]
    master = next((c for c in components if c.get("recurrence-id") is None), None)
    detail: dict[str, Any] = {}
    if master is not None and master.get("rrule") is not None:
        detail["rrule"] = master["rrule"].to_ical().decode()
    excluded: list[str] = []
    if master is not None:
        for prop in master.get("exdate") if isinstance(master.get("exdate"), list) else [master.get("exdate")]:
            excluded.extend(format_value(d.dt, tz) for d in getattr(prop, "dts", []))
    if excluded:
        detail["excluded"] = excluded
    moved = []
    for comp in components:
        recurrence_id = getattr(comp.get("recurrence-id"), "dt", None)
        if recurrence_id is None:
            continue
        item = {
            "recurrence_id": format_value(recurrence_id, tz),
            "start": format_value(comp.start, tz),
            "end": format_value(comp.end, tz),
        }
        if comp.get("summary") is not None:
            item["summary"] = sanitize_short_text(str(comp["summary"]))
        moved.append(item)
    if moved:
        detail["overrides"] = moved
    return detail


def horizon(days: int) -> timedelta:
    """Return a *days*-long default span."""
    return timedelta(days=days)
