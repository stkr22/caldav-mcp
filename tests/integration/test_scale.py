"""A big calendar: queries must be bounded by the server, never by downloading everything."""

from __future__ import annotations

import time
from datetime import date, timedelta

import pytest

from tests.integration.conftest import ics, timed

EVENTS = 1500
pytestmark = [pytest.mark.slow, pytest.mark.timeout(300)]


@pytest.fixture(scope="module")
def big(dav):
    cal = dav.principal().make_calendar(name="scale-test")
    start = date(2026, 1, 1)
    for i in range(EVENTS):
        day = start + timedelta(days=i % 365)
        hour = 8 + i % 9
        title = "Quarterly review" if i % 100 == 0 else f"Meeting {i}"
        cal.save_event(
            ics(
                timed(
                    f"e{i}",
                    title,
                    f"{day:%Y%m%d}T{hour:02d}0000",
                    f"{day:%Y%m%d}T{hour:02d}3000",
                    f"DESCRIPTION:{'padding ' * 100}",
                )
            )
        )
    for n in range(5):
        cal.save_event(ics(timed(f"daily{n}", f"Daily {n}", "20260101T070000", "20260101T071500", "RRULE:FREQ=DAILY")))
    yield cal
    cal.delete()


class Traffic:
    """Counts what goes over the wire during a block."""

    def __init__(self, monkeypatch, dav):
        self.requests: list[str] = []
        self.bytes = 0
        original = type(dav).request

        def counting(client, url, method="GET", *args, **kwargs):
            response = original(client, url, method, *args, **kwargs)
            self.requests.append(method)
            self.bytes += len(getattr(response, "raw", b"") or b"")
            return response

        monkeypatch.setattr(type(dav), "request", counting)


def test_a_week_agenda_is_one_bounded_query(agent, big, dav, monkeypatch):
    traffic = Traffic(monkeypatch, dav)
    started = time.monotonic()
    answer = agent.call("caldav_get_events", start="2026-06-01", end="2026-06-07", calendars=[big.id], limit=100)
    elapsed = time.monotonic() - started
    # About 29 one-off events plus 5 daily series x 7 days; the calendar's other 1,400+ events are never fetched.
    assert 55 <= answer["total"] <= 70 and answer["count"] == answer["total"]
    assert traffic.requests.count("REPORT") == 1 and "GET" not in traffic.requests
    assert traffic.bytes < 150_000, f"{traffic.bytes} bytes moved for one week of a {EVENTS}-event calendar"
    assert elapsed < 5


def test_a_year_is_truncated_with_a_note_and_stays_inside_the_size_ceiling(agent, big):
    answer = agent.call("caldav_get_events", start="2026-01-01", end="2026-12-31", calendars=[big.id], limit=50)
    assert answer["count"] == 50 and answer["total"] > 1500
    assert any("Showing 50 of" in n for n in answer["notes"])
    assert len(str(answer)) < 60_000


def test_asking_for_everything_is_cut_to_the_ceiling_not_flooded(agent, big):
    result = agent.raw(
        "caldav_get_events", start="2026-01-01", end="2026-12-31", calendars=[big.id], limit=500, include=["all"]
    )
    assert len(result.content[0].text.encode()) <= 60_000
    notes = result.structured_content["notes"]
    assert any("left out to keep the answer under 60000 bytes" in n for n in notes)


def test_a_text_search_is_narrowed_by_the_server(agent, big, dav, monkeypatch):
    everything = Traffic(monkeypatch, dav)
    agent.call("caldav_get_events", start="2026-01-01", end="2026-12-31", calendars=[big.id], limit=1)
    full = everything.bytes
    monkeypatch.undo()
    narrowed = Traffic(monkeypatch, dav)
    answer = agent.call(
        "caldav_search_events",
        query="quarterly",
        fields=["summary"],
        start="2026-01-01",
        end="2026-12-31",
        calendars=[big.id],
    )
    assert answer["search"]["path"] == "server (time range and text), re-checked locally"
    assert answer["total"] == 15 and all(e["summary"] == "Quarterly review" for e in answer["events"])
    assert narrowed.bytes < full / 10, f"search moved {narrowed.bytes} bytes against {full} for the whole year"


def test_the_next_occurrence_of_a_title_is_one_cheap_call(agent, big):
    started = time.monotonic()
    # A date-time start looks forward a year; a date-only start would cover just that day.
    answer = agent.call(
        "caldav_search_events", query="quarterly", limit=1, calendars=[big.id], start="2026-03-01T00:00"
    )
    assert answer["count"] == 1 and answer["events"][0]["summary"] == "Quarterly review"
    assert time.monotonic() - started < 15
