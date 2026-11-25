"""What every tool shares: the app context, the guard that turns failures into ToolErrors, and notes."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import ParamSpec

from fastmcp.tools import ToolResult

from caldav_mcp.audit import log_operation
from caldav_mcp.config import MAX_MAX_EVENTS, Config
from caldav_mcp.engine import Fetched
from caldav_mcp.errors import ToolInputError, to_tool_error
from caldav_mcp.events import INCLUDE_FIELDS
from caldav_mcp.sanitize import quoted
from caldav_mcp.session import Session

P = ParamSpec("P")

READ_ONLY = {"read_only_hint": True, "destructive_hint": False, "idempotent_hint": True, "open_world_hint": True}
CREATE = {"read_only_hint": False, "destructive_hint": False, "idempotent_hint": False, "open_world_hint": True}
UPDATE = {"read_only_hint": False, "destructive_hint": False, "idempotent_hint": True, "open_world_hint": True}
DESTRUCTIVE = {"read_only_hint": False, "destructive_hint": True, "idempotent_hint": False, "open_world_hint": True}


@dataclass
class App:
    """Everything a tool needs, built once at startup."""

    config: Config
    session: Session
    clock: Callable[[], datetime] = field(default=lambda: datetime.now().astimezone())

    def now(self) -> datetime:
        """Return the current time in the server zone."""
        return self.clock().astimezone(self.config.tz)


def guarded(name: str) -> Callable[[Callable[P, ToolResult]], Callable[P, ToolResult]]:
    """Turn any failure in a tool into a ToolError and write one audit line."""

    def decorator(fn: Callable[P, ToolResult]) -> Callable[P, ToolResult]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
            started = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                error = to_tool_error(exc, name)
                log_operation(name, "error", (time.monotonic() - started) * 1000, error=type(exc).__name__)
                raise error from exc
            log_operation(name, "ok", (time.monotonic() - started) * 1000)
            return result

        return wrapper

    return decorator


def parse_limit(limit: int | None, default: int) -> int:
    """Validate a caller's ``limit``."""
    if limit is None:
        return default
    if not 1 <= limit <= MAX_MAX_EVENTS:
        raise ToolInputError(f"limit must be between 1 and {MAX_MAX_EVENTS}.")
    return limit


def parse_include(include: list[str] | None) -> frozenset[str]:
    """Validate ``include``; ``all`` selects every optional field."""
    if not include:
        return frozenset()
    chosen = {item.strip().lower() for item in include}
    if "all" in chosen:
        return frozenset(INCLUDE_FIELDS)
    unknown = chosen - set(INCLUDE_FIELDS)
    if unknown:
        raise ToolInputError(
            f"include: unknown {', '.join(quoted(u, 30) for u in sorted(unknown))}. "
            f"Use any of: {', '.join(INCLUDE_FIELDS)}, all."
        )
    return frozenset(chosen)


def fetch_notes(fetched: Fetched, *, total: int, returned: int, filtered: bool) -> list[str]:
    """Say what the answer cannot say for itself: truncation, hidden items, partial failure, empty-and-why."""
    notes: list[str] = []
    if returned < total:
        notes.append(
            f"Showing {returned} of {total} matching events. "
            f"Narrow the range or raise `limit` (at most {MAX_MAX_EVENTS})."
        )
    if fetched.errors:
        names = ", ".join(repr(e["calendar"]) for e in fetched.errors)
        notes.append(f"INCOMPLETE: could not read calendar(s) {names}; events from them are missing. See `errors`.")
    if fetched.cancelled_hidden:
        notes.append(f"{fetched.cancelled_hidden} cancelled event(s) are not shown.")
    if fetched.unreadable:
        notes.append(f"{fetched.unreadable} event(s) without a start time could not be read and are not shown.")
    if filtered and fetched.plan.reasons:
        notes.append("Matching note: " + "; ".join(fetched.plan.reasons) + ".")
    if total == 0 and not fetched.errors:
        where = ", ".join(repr(c) for c in fetched.calendars)
        if filtered and not fetched.plan.terms:
            notes.append(
                f"Nothing matched: {fetched.considered} event(s) exist in this range in {where}, "
                "none satisfy the filter."
            )
        elif filtered:
            notes.append(
                f"Nothing matched: no event in this range in {where} contains the searched text on the server."
            )
        else:
            notes.append(f"No events in this range in {where}.")
    return notes
