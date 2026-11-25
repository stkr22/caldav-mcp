"""Typed tool failures and their translation into messages an agent can act on.

Every failure leaves a tool as :class:`fastmcp.exceptions.ToolError`, which the
protocol reports with ``isError=true``. The message is written in this server's
own voice, so any value it repeats (a UID, a calendar name, a server's own error
text) goes through :func:`caldav_mcp.sanitize.quoted` first.
"""

from __future__ import annotations

import logging

import niquests.exceptions as net
from caldav.lib import error as dav
from fastmcp.exceptions import ToolError

from caldav_mcp.sanitize import defuse_auto_fetch, quoted

log = logging.getLogger("caldav-mcp")

_MAX_SERVER_TEXT = 300


class ToolInputError(ValueError):
    """The call's arguments are wrong. The message says how to correct them."""


class EventNotFoundError(LookupError):
    """No event with that UID (or recurrence) exists."""


class CalendarNotFoundError(LookupError):
    """No calendar with that name exists."""


class ConflictError(RuntimeError):
    """The event changed on the server since it was read; nothing was written."""


class ResultTooLargeError(RuntimeError):
    """An answer cannot be shrunk below the size ceiling."""


_STATUS_HINTS: dict[int, str] = {
    401: "Check CALDAV_USERNAME and CALDAV_PASSWORD; most hosted services want an app-specific password.",
    403: "The server accepted the login and refused the action; this is usually a permission on the calendar.",
    404: "The event or calendar is gone; list it again.",
    405: "The server does not allow that method here; CALDAV_URL may not be a CalDAV endpoint.",
    409: "The parent collection does not exist; the calendar may have been deleted.",
    412: "The event changed on the server since it was read, so nothing was written. Read it again and retry.",
    415: "The server rejected the content type; a proxy in front of it may be rewriting requests.",
    423: "The event is locked on the server.",
    429: "The server is rate limiting this account; wait a moment and retry.",
    507: "The account is out of storage quota.",
}


def _server_text(exc: dav.DAVError) -> str:
    reason = getattr(exc, "reason", "") or ""
    if not reason or reason == "no reason":
        return ""
    return " Server said (untrusted text): " + quoted(defuse_auto_fetch(str(reason)), _MAX_SERVER_TEXT)


_OWN_ERRORS = (ToolInputError, EventNotFoundError, CalendarNotFoundError, ConflictError, ResultTooLargeError)


def _dav_message(exc: dav.DAVError) -> str:
    """Describe a failed CalDAV request, naming the status and what to do about it."""
    status = getattr(exc, "status", None)
    if isinstance(exc, dav.ETagMismatchError | dav.ScheduleTagMismatchError):
        return _STATUS_HINTS[412]
    if isinstance(exc, dav.NotFoundError):
        return "Not found on the server. " + _STATUS_HINTS[404]
    if isinstance(exc, dav.AuthorizationError):
        hint = _STATUS_HINTS.get(status or 0, _STATUS_HINTS[401])
        return f"The CalDAV server refused the request (HTTP {status or '401/403'}). {hint}"
    hint = _STATUS_HINTS.get(status or 0, "")
    shown = f" (HTTP {status})" if status else ""
    return f"The CalDAV server returned an error{shown}. {hint}{_server_text(exc)}".strip()


def _network_message(exc: BaseException) -> str | None:
    if isinstance(exc, net.Timeout):
        return "The CalDAV server did not answer in time. Retry; if it keeps happening the server is slow or down."
    if isinstance(exc, net.ConnectionError | net.SSLError):
        return "Could not reach the CalDAV server (connection or TLS failure); this is not a problem with the request."
    return None


def to_tool_error(exc: BaseException, tool: str) -> ToolError:
    """Translate any exception into a ToolError the agent can act on."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, _OWN_ERRORS):
        return ToolError(str(exc))
    if isinstance(exc, dav.DAVError):
        return ToolError(_dav_message(exc))
    network = _network_message(exc)
    if network is not None:
        return ToolError(network)
    log.error("Unhandled error in %s", tool, exc_info=exc)
    return ToolError("Internal error; details are in the server log.")
