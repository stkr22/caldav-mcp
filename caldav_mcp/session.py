"""The one CalDAV connection, and the calendars on it.

A personal assistant talks to one account, so there is one :class:`DAVClient`
and one lock: each tool call is a short critical section, bounded by the request
timeout. The calendar list is cached for a few minutes - listing it is a PROPFIND
that every call would otherwise repeat - and refreshed when a name is not found.

Calendars are addressed by **id** (the last path segment of the collection URL:
stable, unique, URL-safe) or by display name, compared case-insensitively. A
display name is chosen by whoever shared the calendar and need not be unique, so
an ambiguous name is an error that lists the ids rather than a silent first
match; and every name that reaches the agent goes through the sanitiser.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from caldav.davclient import DAVClient

from caldav_mcp.config import Config
from caldav_mcp.errors import CalendarNotFoundError, ToolInputError
from caldav_mcp.filters import fold
from caldav_mcp.sanitize import quoted, sanitize_short_text

CALENDAR_CACHE_SECONDS = 300


@dataclass(frozen=True)
class CalendarInfo:
    """A calendar that can hold events."""

    id: str
    name: str
    url: str
    components: tuple[str, ...]
    handle: Any = None  # the caldav Calendar; kept out of comparisons and reprs

    def label(self) -> str:
        """Return the sanitised display name, falling back to the id."""
        return sanitize_short_text(self.name) or self.id


def _supports_events(components: list[str] | None) -> bool:
    # A server that does not say is assumed to accept events; hiding a calendar is the worse error.
    return not components or "VEVENT" in [c.upper() for c in components]


class Session:
    """Owns the client, the lock and the calendar cache."""

    def __init__(self, config: Config, client: DAVClient | None = None) -> None:
        """Create the session; the connection is opened on first use (or injected for tests)."""
        self._config = config
        self._client = client
        self._lock = threading.RLock()
        self._calendars: list[CalendarInfo] = []
        self._fetched_at = 0.0

    @contextmanager
    def operation(self) -> Generator[DAVClient]:
        """Hold the connection for one tool call."""
        with self._lock:
            yield self._connection()

    def _connection(self) -> DAVClient:
        if self._client is None:
            cfg = self._config
            self._client = DAVClient(
                url=cfg.url,
                username=cfg.username,
                password=cfg.password,
                timeout=cfg.timeout,
                ssl_verify_cert=cfg.verify_ssl,
                enable_rfc6764=False,  # the URL is configured; never go looking for another server
            )
        return self._client

    def invalidate(self) -> None:
        """Forget the cached calendar list."""
        with self._lock:
            self._fetched_at = 0.0

    def calendars(self, *, refresh: bool = False) -> list[CalendarInfo]:
        """Return the calendars that can hold events."""
        with self._lock:
            fresh = time.monotonic() - self._fetched_at < CALENDAR_CACHE_SECONDS
            if refresh or not fresh or not self._calendars:
                self._calendars = self._load()
                self._fetched_at = time.monotonic()
            return list(self._calendars)

    def _load(self) -> list[CalendarInfo]:
        found: list[CalendarInfo] = []
        for cal in self._connection().principal().calendars():
            try:
                components = [str(c) for c in (cal.get_supported_components() or [])]
            except Exception:
                components = []
            if not _supports_events(components):
                continue
            cal_id = str(cal.id or str(cal.url).rstrip("/").rsplit("/", 1)[-1])
            found.append(
                CalendarInfo(cal_id, str(cal.get_display_name() or cal_id), str(cal.url), tuple(components), cal)
            )
        return found

    def resolve(self, wanted: list[str] | None) -> list[CalendarInfo]:
        """Return the named calendars (ids or case-insensitive names), or all of them."""
        available = self.calendars()
        if not available:
            raise CalendarNotFoundError("The account has no calendars that can hold events.")
        if not wanted:
            return available
        picked: list[CalendarInfo] = []
        for token in wanted:
            found = self._find(available, token)
            if found is None:
                available = self.calendars(refresh=True)
                found = self._find(available, token)
            if found is None:
                raise CalendarNotFoundError(
                    f"No calendar {quoted(token, 60)!r}. Available: "
                    + ", ".join(f"{c.label()!r} (id {c.id})" for c in available)
                    + "."
                )
            if found not in picked:
                picked.append(found)
        return picked

    @staticmethod
    def _find(available: list[CalendarInfo], token: str) -> CalendarInfo | None:
        token = token.strip()
        by_id = [c for c in available if c.id == token]
        if by_id:
            return by_id[0]
        named = [c for c in available if fold(c.name) == fold(token)]
        if len(named) > 1:
            raise ToolInputError(
                f"The calendar name {quoted(token, 60)!r} is ambiguous; use one of these ids: "
                + ", ".join(f"{c.id}" for c in named)
                + "."
            )
        return named[0] if named else None

    def for_write(self, requested: str, default: str) -> CalendarInfo:
        """Choose the calendar a new event goes to: the one asked for, the configured default, or the only one."""
        wanted = requested.strip() or default.strip()
        if wanted:
            return self.resolve([wanted])[0]
        available = self.calendars()
        if len(available) == 1:
            return available[0]
        if not available:
            raise CalendarNotFoundError("The account has no calendars that can hold events.")
        raise ToolInputError(
            "Which calendar? Pass `calendar` (a name or id). Available: "
            + ", ".join(f"{c.label()!r} (id {c.id})" for c in available)
            + "."
        )
