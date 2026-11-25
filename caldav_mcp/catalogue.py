"""The tools this server can register, declared rather than discovered.

Declared, because the tool filter must be able to answer "is this a name you
have?" *before* anything is registered - and under read-only mode the write
tools are never registered at all. Deriving the catalogue from what reached
``mcp.tool`` would make ``CALDAV_ALLOW_TOOLS=caldav_delete_event`` report an
unknown tool in read-only mode, which is wrong: the tool exists and read-only is
suppressing it. ``tests/unit/test_catalogue.py`` asserts these lists and the
registered tools are the same set.
"""

PREFIX = "caldav_"

#: Always registered. Every one carries ``readOnlyHint``.
READ_TOOLS: tuple[str, ...] = (
    "caldav_get_event_by_uid",
    "caldav_get_events",
    "caldav_get_freebusy",
    "caldav_list_calendars",
    "caldav_search_events",
)

#: Registered unless read-only mode is on.
WRITE_TOOLS: tuple[str, ...] = (
    "caldav_add_attendee",
    "caldav_create_event",
    "caldav_delete_event",
    "caldav_move_event",
    "caldav_remove_attendee",
    "caldav_update_event",
)

ALL_TOOLS: tuple[str, ...] = (*READ_TOOLS, *WRITE_TOOLS)

#: What ``CALDAV_ALLOW_TOOLS=essential`` selects: find, read, check availability,
#: add and correct an event. Left out on purpose: everything irreversible
#: (delete, move) and the attendee tools, which can cause invitations to go out.
ESSENTIAL_TOOLS: tuple[str, ...] = (
    "caldav_list_calendars",
    "caldav_get_events",
    "caldav_search_events",
    "caldav_get_event_by_uid",
    "caldav_get_freebusy",
    "caldav_create_event",
    "caldav_update_event",
)
