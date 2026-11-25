"""Integration fixtures: a real CalDAV server (Radicale), a fixed clock, an in-process MCP client.

Start Radicale with the dev-container compose file (``radicale`` service, 127.0.0.1:5232) or, without
Docker, ``RADICALE_PORT=5233 tests/integration/radicale-local.sh``. Point the suite at it with
``CALDAV_TEST_URL`` (default ``http://127.0.0.1:5232``). The suite is skipped when nothing answers,
unless ``CALDAV_REQUIRE_INTEGRATION=1`` (CI sets it), in which case an unreachable server fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from caldav.davclient import DAVClient
from fastmcp import Client
from fastmcp.exceptions import ToolError

from caldav_mcp.app import create_server
from caldav_mcp.config import Config, load_config

BASE = os.environ.get("CALDAV_TEST_URL", "http://127.0.0.1:5232").rstrip("/")
USER = os.environ.get("CALDAV_TEST_USER", "testuser")
PASSWORD = os.environ.get("CALDAV_TEST_PASSWORD", "testpass")
BERLIN = ZoneInfo("Europe/Berlin")
#: Wednesday 25 November 2026, mid-afternoon. Every relative date in the suite is relative to this.
NOW = datetime(2026, 11, 25, 15, 30, tzinfo=BERLIN)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def dav() -> Iterator[DAVClient]:
    client = DAVClient(url=f"{BASE}/{USER}/", username=USER, password=PASSWORD, timeout=15, enable_rfc6764=False)
    try:
        client.principal().calendars()
    except Exception as exc:
        if os.environ.get("CALDAV_REQUIRE_INTEGRATION") == "1":
            raise
        pytest.skip(f"no CalDAV server at {BASE} ({type(exc).__name__}); see tests/integration/conftest.py")
    yield client


@pytest.fixture
def config() -> Config:
    env = {
        "CALDAV_URL": f"{BASE}/{USER}/",
        "CALDAV_USERNAME": USER,
        "CALDAV_PASSWORD": PASSWORD,
        "CALDAV_MCP_API_KEY": "integration-test-key-0123456789",
        "TZ": "Europe/Berlin",
    }
    return load_config(env)


@pytest.fixture
def make_calendar(dav: DAVClient) -> Iterator[Callable[..., Any]]:
    created: list[Any] = []

    def make(name: str | None = None) -> Any:
        cal = dav.principal().make_calendar(name=name or f"it-{uuid.uuid4().hex[:8]}")
        created.append(cal)
        return cal

    yield make
    for cal in created:
        with contextlib.suppress(Exception):  # best-effort cleanup
            cal.delete()


@pytest.fixture
def cal(make_calendar: Callable[..., Any]) -> Any:
    return make_calendar()


@pytest.fixture
def server(config: Config, dav: DAVClient, cal: Any):
    """The MCP server with the clock fixed at NOW; depends on `cal` so a calendar exists."""
    return create_server(config, clock=lambda: NOW)


class Agent:
    """Calls tools the way an MCP client does, and parses the JSON answer."""

    def __init__(self, mcp: Any) -> None:
        self.mcp = mcp

    def raw(self, tool: str, **args: Any) -> Any:
        async def run() -> Any:
            async with Client(self.mcp) as client:
                return await client.call_tool(tool, args, raise_on_error=False)

        return asyncio.run(run())

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        result = self.raw(tool, **args)
        if result.is_error:
            raise ToolError(result.content[0].text)
        return json.loads(result.content[-1].text)

    def fails(self, tool: str, **args: Any) -> str:
        result = self.raw(tool, **args)
        assert result.is_error, f"expected {tool} to fail, got {result.content[0].text[:200]}"
        return result.content[0].text


@pytest.fixture
def agent(server: Any) -> Agent:
    return Agent(server)


def ics(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def vevent(uid: str, summary: str, *lines: str) -> str:
    body = [f"UID:{uid}", "DTSTAMP:20260101T000000Z", f"SUMMARY:{summary}", *lines]
    return "BEGIN:VEVENT\r\n" + "".join(f"{line}\r\n" for line in body) + "END:VEVENT\r\n"


def timed(uid: str, summary: str, start: str, end: str, *extra: str) -> str:
    return vevent(uid, summary, f"DTSTART;TZID=Europe/Berlin:{start}", f"DTEND;TZID=Europe/Berlin:{end}", *extra)


def allday(uid: str, summary: str, first: str, last_exclusive: str, *extra: str) -> str:
    return vevent(uid, summary, f"DTSTART;VALUE=DATE:{first}", f"DTEND;VALUE=DATE:{last_exclusive}", *extra)


def titles(answer: dict[str, Any]) -> list[str]:
    return [e["summary"] for e in answer["events"]]


@pytest.fixture
def seeded(cal: Any) -> Any:
    """A calendar with the awkward cases in it."""
    put = cal.save_event
    put(
        ics(
            timed(
                "dentist",
                "Dentist",
                "20261126T100000",
                "20261126T110000",
                "LOCATION:Praxis Dr. Roth",
                'ATTENDEE;CN="Anna Meier":mailto:anna@example.com',
                "DESCRIPTION:Bring the X-rays",
            )
        )
    )
    put(ics(allday("birthday", "Birthday", "20261128", "20261129")))
    put(ics(timed("dinner", "Dinner", "20261129T180000", "20261129T190000")))
    put(ics(timed("night", "Night shift", "20261126T230000", "20261127T010000")))
    put(ics(vevent("floaty", "Floaty", "DTSTART:20261127T120000", "DTEND:20261127T130000")))
    put(ics(timed("zahn", "Zahn\u00e4rztin M\u00fcller", "20261202T090000", "20261202T100000")))
    put(ics(timed("old", "Old meeting", "20261020T090000", "20261020T100000")))
    put(
        ics(
            timed(
                "standup",
                "Standup",
                "20261102T100000",
                "20261102T103000",
                "RRULE:FREQ=WEEKLY;COUNT=10",
                "EXDATE;TZID=Europe/Berlin:20261116T100000",
            ),
            timed(
                "standup",
                "Standup (moved)",
                "20261123T150000",
                "20261123T153000",
                "RECURRENCE-ID;TZID=Europe/Berlin:20261123T100000",
            ),
        )
    )
    return cal
