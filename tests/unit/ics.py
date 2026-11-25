"""Helpers for building iCalendar components in tests."""

from icalendar import Calendar

BERLIN = "Europe/Berlin"


def component(*lines: str):
    """Parse the given VEVENT body lines into an icalendar component."""
    text = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//test//EN",
            "BEGIN:VEVENT",
            *lines,
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    return Calendar.from_ical(text).walk("VEVENT")[0]


def timed(summary="Dentist", start="20261126T100000", end="20261126T110000", *extra: str):
    return component(
        "UID:u1",
        "DTSTAMP:20260101T000000Z",
        f"SUMMARY:{summary}",
        f"DTSTART;TZID={BERLIN}:{start}",
        f"DTEND;TZID={BERLIN}:{end}",
        *extra,
    )
