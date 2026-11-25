"""Attendee tools. Adding an attendee can make the server send an invitation, so deployments may switch them off."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field

from caldav_mcp import ops
from caldav_mcp.errors import ToolInputError
from caldav_mcp.inputs import MAX_SUMMARY, text_field
from caldav_mcp.results import untrusted_result
from caldav_mcp.tools.common import CREATE, DESTRUCTIVE, App, guarded

_ROLES = ("REQ-PARTICIPANT", "OPT-PARTICIPANT", "CHAIR", "NON-PARTICIPANT")
Uid = Annotated[str, Field(description="The event's uid, exactly as another tool returned it.")]
Email = Annotated[str, Field(description="The attendee's email address.")]
Scope = Annotated[
    str | None,
    Field(description="For a repeating event pass 'series': attendees apply to every occurrence. Leave out otherwise."),
]
Etag = Annotated[
    str | None, Field(description="The `etag` from caldav_get_event_by_uid; refuses the write if it changed.")
]
CalendarOpt = Annotated[str | None, Field(description="Calendar name or id. Omit when the uid is enough.")]


def register(mcp: FastMCP, app: App, enabled: frozenset[str]) -> None:
    """Register the attendee tools that are enabled."""
    if "caldav_add_attendee" in enabled:

        @mcp.tool(name="caldav_add_attendee", annotations=CREATE)
        @guarded("caldav_add_attendee")
        def caldav_add_attendee(
            uid: Uid,
            email: Email,
            name: Annotated[str | None, Field(description="Display name.")] = None,
            role: Annotated[str, Field(description=f"One of {', '.join(_ROLES)}.")] = "REQ-PARTICIPANT",
            scope: Scope = None,
            etag: Etag = None,
            calendar: CalendarOpt = None,
        ) -> ToolResult:
            """Add an attendee to an event.

            Depending on the server, this can email the person an invitation. Adding someone already on
            the event changes nothing. The answer lists the attendees as stored.
            """
            role = role.strip().upper()
            if role not in _ROLES:
                raise ToolInputError(f"role must be one of: {', '.join(_ROLES)}.")
            found = ops.locate(app, uid, calendar)
            display = text_field(name, "name", MAX_SUMMARY)
            return untrusted_result(
                ops.change_attendee(app, found, email=email, add=True, name=display, role=role, scope=scope, etag=etag)
            )

    if "caldav_remove_attendee" in enabled:

        @mcp.tool(name="caldav_remove_attendee", annotations=DESTRUCTIVE)
        @guarded("caldav_remove_attendee")
        def caldav_remove_attendee(
            uid: Uid, email: Email, scope: Scope = None, etag: Etag = None, calendar: CalendarOpt = None
        ) -> ToolResult:
            """Remove an attendee from an event. The answer lists the attendees left."""
            found = ops.locate(app, uid, calendar)
            return untrusted_result(
                ops.change_attendee(app, found, email=email, add=False, name=None, role="", scope=scope, etag=etag)
            )
