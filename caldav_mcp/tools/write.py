"""Write tools: create, update, delete and move an event."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field

from caldav_mcp import ops
from caldav_mcp.errors import ToolInputError
from caldav_mcp.inputs import (
    MAX_DESCRIPTION,
    MAX_LOCATION,
    MAX_SUMMARY,
    categories_field,
    end_exclusive,
    new_event_when,
    parse_value,
    reminders_field,
    rrule_field,
    text_field,
)
from caldav_mcp.results import untrusted_result
from caldav_mcp.series import CLEARABLE, Edits
from caldav_mcp.tools.common import CREATE, DESTRUCTIVE, UPDATE, App, guarded

Uid = Annotated[str, Field(description="The event's uid, exactly as another tool returned it.")]
CalendarOpt = Annotated[str | None, Field(description="Calendar name or id. Omit when the uid is enough.")]
Scope = Annotated[
    str | None,
    Field(
        description="REQUIRED for a repeating event, never guessed: 'series' (every occurrence), 'instance' (one "
        "occurrence, needs `instance`) or 'this_and_future' (that occurrence and all later ones, needs `instance`). "
        "Leave out for a one-off event."
    ),
]
Instance = Annotated[
    str | None,
    Field(
        description="Which occurrence, for scope 'instance' or 'this_and_future': its `recurrence_id` as "
        "caldav_get_events shows it (or just its date, if only one occurrence starts that day)."
    ),
]
Etag = Annotated[
    str | None,
    Field(
        description="The `etag` from caldav_get_event_by_uid. If the event changed since, nothing is written. "
        "Optional: the write is always checked against the version just read."
    ),
]
Summary = Annotated[str, Field(description="The title, one line.")]
Reminders = Annotated[
    list[int] | None,
    Field(description="Reminders, in minutes before the start, e.g. [15, 1440]. Replaces any existing ones."),
]


def _edits(
    app: App,
    *,
    summary: str | None,
    start: str | None,
    end: str | None,
    duration_minutes: int | None,
    location: str | None,
    description: str | None,
    categories: list[str] | None,
    reminders: list[int] | None,
    rrule: str | None,
    clear: list[str] | None,
) -> Edits:
    cfg, today = app.config, ops.today_label(app)
    wanted = frozenset(c.strip().lower() for c in (clear or []))
    if wanted - set(CLEARABLE):
        raise ToolInputError(
            f"clear: unknown {', '.join(sorted(wanted - set(CLEARABLE)))}. Use any of: {', '.join(CLEARABLE)}."
        )
    given = {
        "location": location,
        "description": description,
        "categories": categories,
        "reminders": reminders,
        "rrule": rrule,
    }
    both = [name for name in wanted if given.get(name) is not None]
    if both:
        raise ToolInputError(f"{', '.join(both)} is both set and listed in `clear`; do one or the other.")
    if end is not None and duration_minutes is not None:
        raise ToolInputError("Give `end` or `duration_minutes`, not both.")
    edits = Edits(
        summary=text_field(summary, "summary", MAX_SUMMARY),
        location=text_field(location, "location", MAX_LOCATION),
        description=text_field(description, "description", MAX_DESCRIPTION, multiline=True),
        categories=categories_field(categories),
        reminders=reminders_field(reminders),
        rrule=rrule_field(rrule),
        start=parse_value(start, cfg.tz, today, "start") if start else None,
        end=end_exclusive(parse_value(end, cfg.tz, today, "end")) if end else None,
        duration_minutes=duration_minutes,
        clear=wanted,
    )
    if edits == Edits():
        raise ToolInputError("Nothing to change: give at least one field to set, or list fields in `clear`.")
    return edits


def register(mcp: FastMCP, app: App, enabled: frozenset[str]) -> None:
    """Register the write tools that are enabled."""
    cfg = app.config

    if "caldav_create_event" in enabled:

        @mcp.tool(name="caldav_create_event", annotations=CREATE)
        @guarded("caldav_create_event")
        def caldav_create_event(
            summary: Summary,
            start: Annotated[
                str,
                Field(
                    description="Exact ISO date-time (2026-11-28T15:00) for a timed event, or a date "
                    "(2026-11-28) for an all-day event. Words like 'tomorrow' are refused: work out the date "
                    "from the `now` that caldav_list_calendars gives."
                ),
            ],
            end: Annotated[
                str | None,
                Field(
                    description="Same format. For an all-day event a date-only end is its LAST day. "
                    "Omit for one hour (timed) or one day."
                ),
            ] = None,
            duration_minutes: Annotated[
                int | None, Field(description="Length of a timed event instead of `end`.")
            ] = None,
            calendar: Annotated[
                str | None, Field(description="Calendar name or id. Omit to use the default (or the only) calendar.")
            ] = None,
            location: Annotated[str | None, Field(description="Where, one line.")] = None,
            description: Annotated[str | None, Field(description="Notes; may have several lines.")] = None,
            categories: Annotated[list[str] | None, Field(description="Category labels.")] = None,
            reminders_minutes_before: Reminders = None,
            rrule: Annotated[
                str | None,
                Field(
                    description="Repeat rule, e.g. 'FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10' or 'FREQ=YEARLY'. "
                    "Omit for a one-off."
                ),
            ] = None,
            check_conflicts: Annotated[
                bool, Field(description="Report timed events this overlaps (never blocks).")
            ] = True,
        ) -> ToolResult:
            """Create an event, and report what it overlaps.

            A date-only `start` makes an all-day event. The answer is the event as stored (with its uid and
            ETag), plus `conflicts` if a timed event overlaps. Creating the same title and time twice
            creates nothing the second time (`created: false`), so a retry is safe.
            """
            title = text_field(summary, "summary", MAX_SUMMARY, required=True) or ""
            when = new_event_when(start, end, duration_minutes, tz=cfg.tz, today=ops.today_label(app))
            extras = {
                "location": text_field(location, "location", MAX_LOCATION),
                "description": text_field(description, "description", MAX_DESCRIPTION, multiline=True),
                "categories": categories_field(categories),
                "reminders": reminders_field(reminders_minutes_before),
                "rrule": rrule_field(rrule),
            }
            info = app.session.for_write(calendar or "", cfg.default_calendar)
            return untrusted_result(
                ops.create(app, info=info, summary=title, when=when, extras=extras, check_conflicts=check_conflicts)
            )

    if "caldav_update_event" in enabled:

        @mcp.tool(name="caldav_update_event", annotations=UPDATE)
        @guarded("caldav_update_event")
        def caldav_update_event(
            uid: Uid,
            scope: Scope = None,
            instance: Instance = None,
            etag: Etag = None,
            calendar: CalendarOpt = None,
            summary: Annotated[str | None, Field(description="New title.")] = None,
            start: Annotated[
                str | None,
                Field(
                    description="New start (exact ISO date or date-time). Moving the start keeps the event's length."
                ),
            ] = None,
            end: Annotated[str | None, Field(description="New end (date-only = last day, inclusive).")] = None,
            duration_minutes: Annotated[int | None, Field(description="New length of a timed event.")] = None,
            location: Annotated[str | None, Field(description="New location.")] = None,
            description: Annotated[str | None, Field(description="New notes.")] = None,
            categories: Annotated[list[str] | None, Field(description="New category labels (replaces).")] = None,
            reminders_minutes_before: Reminders = None,
            rrule: Annotated[str | None, Field(description="New repeat rule (series or this_and_future only).")] = None,
            clear: Annotated[
                list[str] | None,
                Field(
                    description=f"Fields to remove: any of {', '.join(CLEARABLE)}. "
                    "Omitting a field leaves it unchanged."
                ),
            ] = None,
        ) -> ToolResult:
            """Change an event. Only the fields you give are changed; nothing else is touched.

            Use for "move the meeting with X" (start, optionally end), renaming, relocating. To remove a
            field list it in `clear` (an empty string is refused). For a repeating event `scope` is
            required: 'series', 'instance' or 'this_and_future'. Exceptions already made to a series
            (moved or cancelled occurrences) are kept. The answer is the event as stored afterwards.
            """
            edits = _edits(
                app,
                summary=summary,
                start=start,
                end=end,
                duration_minutes=duration_minutes,
                location=location,
                description=description,
                categories=categories,
                reminders=reminders_minutes_before,
                rrule=rrule,
                clear=clear,
            )
            found = ops.locate(app, uid, calendar)
            return untrusted_result(ops.update(app, found, scope=scope, instance=instance, etag=etag, edits=edits))

    if "caldav_delete_event" in enabled:

        @mcp.tool(name="caldav_delete_event", annotations=DESTRUCTIVE)
        @guarded("caldav_delete_event")
        def caldav_delete_event(
            uid: Uid, scope: Scope = None, instance: Instance = None, etag: Etag = None, calendar: CalendarOpt = None
        ) -> ToolResult:
            """Delete an event, one occurrence of a repeating event, or an occurrence and everything after it.

            For a repeating event `scope` is required; deleting 'series' removes every occurrence. The
            answer says exactly what was removed. This cannot be undone.
            """
            return untrusted_result(
                ops.delete(app, ops.locate(app, uid, calendar), scope=scope, instance=instance, etag=etag)
            )

    if "caldav_move_event" in enabled:

        @mcp.tool(name="caldav_move_event", annotations=DESTRUCTIVE)
        @guarded("caldav_move_event")
        def caldav_move_event(
            uid: Uid,
            target_calendar: Annotated[str, Field(description="Calendar name or id to move it to.")],
            source_calendar: Annotated[str | None, Field(description="Calendar it is in now. Omit to find it.")] = None,
        ) -> ToolResult:
            """Move an event (a whole series, if it repeats) to another calendar, keeping its uid.

            Uses the server's own MOVE where it has one, otherwise copies and then deletes, undoing the copy if
            the delete fails.
            """
            target = app.session.resolve([target_calendar])[0]
            return untrusted_result(ops.move(app, ops.locate(app, uid, source_calendar), target))
