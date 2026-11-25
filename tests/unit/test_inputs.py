"""What an agent may write: exact values, strict shapes, corrections instead of guesses."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from caldav_mcp.errors import ToolInputError
from caldav_mcp.inputs import (
    categories_field,
    email_field,
    end_exclusive,
    new_event_when,
    parse_value,
    reminders_field,
    rrule_field,
    text_field,
)

BERLIN = ZoneInfo("Europe/Berlin")
TODAY = "Wednesday 2026-11-25"


def when(start, end=None, minutes=None):
    return new_event_when(start, end, minutes, tz=BERLIN, today=TODAY)


class TestText:
    def test_none_means_not_given(self):
        assert text_field(None, "summary", 10) is None

    def test_required(self):
        with pytest.raises(ToolInputError, match="summary is required"):
            text_field(None, "summary", 10, required=True)

    def test_empty_is_refused_with_the_way_to_clear(self):
        with pytest.raises(ToolInputError, match="clear"):
            text_field("  ", "location", 10)

    def test_single_line_fields_refuse_newlines_and_controls(self):
        for bad in ("a\nb", "a\x00b", "a\x1bb"):
            with pytest.raises(ToolInputError, match="single line"):
                text_field(bad, "summary", 100)

    def test_multiline_fields_keep_paragraphs_and_normalise_line_endings(self):
        assert text_field("a\r\nb\rc", "description", 100, multiline=True) == "a\nb\nc"

    def test_multiline_still_refuses_controls(self):
        with pytest.raises(ToolInputError):
            text_field("a\x00b", "description", 100, multiline=True)

    def test_invisible_characters_are_removed_not_stored(self):
        assert text_field("Den\u200btist\u202e", "summary", 100) == "Dentist"

    def test_length_limit_is_stated(self):
        with pytest.raises(ToolInputError, match="at most 5"):
            text_field("abcdef", "summary", 5)


class TestLists:
    def test_categories(self):
        assert categories_field(["a", " b "]) == ["a", "b"]
        assert categories_field(None) is None
        for bad in ([], ["a,b"], [""], ["x"] * 11):
            with pytest.raises(ToolInputError):
                categories_field(bad)

    def test_reminders_are_sorted_unique_whole_minutes(self):
        assert reminders_field([60, 10, 10]) == [10, 60]
        bad_values: list[Any] = [[], [-1], [40_321], [True], [1, 2, 3, 4, 5, 6], [1.5]]
        for bad in bad_values:
            with pytest.raises(ToolInputError):
                reminders_field(bad)

    @pytest.mark.parametrize("value", ["Anna@Example.com", "mailto:anna@example.com", " a.b+c@sub.example.org "])
    def test_good_addresses_are_normalised(self, value):
        assert email_field(value) == value.strip().lower().removeprefix("mailto:")

    @pytest.mark.parametrize(
        "value",
        ["", "anna", "a@b", "a b@example.com", "a@example.com\nBCC:x@y.zz", "<a@example.com>", "a@@example.com"],
    )
    def test_bad_addresses_are_refused(self, value):
        with pytest.raises(ToolInputError):
            email_field(value)


class TestRrule:
    @pytest.mark.parametrize(
        "rule",
        [
            "FREQ=WEEKLY",
            "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10",
            "RRULE:FREQ=DAILY;INTERVAL=2",
            "FREQ=YEARLY;UNTIL=20301231T000000Z",
            "FREQ=MONTHLY;BYMONTHDAY=1;BYSETPOS=1",
        ],
    )
    def test_usable_rules(self, rule):
        assert (rrule_field(rule) or "").startswith("FREQ=")

    @pytest.mark.parametrize(
        "rule",
        [
            "",
            "garbage",
            "FREQ=SECONDLY",
            "FREQ=HOURLY",
            "COUNT=3",
            "FREQ=DAILY;COUNT=0",
            "FREQ=DAILY;COUNT=5000",
            "FREQ=DAILY;COUNT=3;UNTIL=20301231T000000Z",
            "FREQ=DAILY;BYSECOND=1",
            "FREQ=DAILY\nATTENDEE:x",
        ],
    )
    def test_unusable_rules_are_refused_with_an_example(self, rule):
        with pytest.raises(ToolInputError, match="Example"):
            rrule_field(rule)

    def test_none_is_not_given(self):
        assert rrule_field(None) is None


class TestValues:
    def test_dates_and_datetimes(self):
        assert parse_value("2026-11-28", BERLIN, TODAY, "start") == date(2026, 11, 28)
        assert parse_value("2026-11-28T15:00", BERLIN, TODAY, "start") == datetime(2026, 11, 28, 15, tzinfo=BERLIN)
        assert parse_value("2026-11-28T14:00Z", BERLIN, TODAY, "start") == datetime(2026, 11, 28, 15, tzinfo=BERLIN)

    @pytest.mark.parametrize("bad", ["tomorrow", "next friday", "15:00", "28.11.2026", "2026-11-31", ""])
    def test_words_are_refused_and_today_is_named(self, bad):
        with pytest.raises(ToolInputError, match="Wednesday 2026-11-25"):
            parse_value(bad, BERLIN, TODAY, "start")

    def test_end_exclusive(self):
        assert end_exclusive(date(2026, 11, 28)) == date(2026, 11, 29)
        stamp = datetime(2026, 11, 28, 15, tzinfo=BERLIN)
        assert end_exclusive(stamp) == stamp


class TestNewEventWhen:
    def test_timed_defaults_to_one_hour(self):
        w = when("2026-11-28T15:00")
        assert (w.end - w.start) == timedelta(hours=1) and not w.all_day

    def test_duration_and_end(self):
        assert when("2026-11-28T15:00", minutes=45).end == datetime(2026, 11, 28, 15, 45, tzinfo=BERLIN)
        assert when("2026-11-28T15:00", "2026-11-28T17:30").end == datetime(2026, 11, 28, 17, 30, tzinfo=BERLIN)

    def test_date_only_is_an_all_day_event_of_one_day(self):
        w = when("2026-11-28")
        assert w.all_day and (w.start, w.end) == (date(2026, 11, 28), date(2026, 11, 29))

    def test_date_only_end_is_the_last_day_inclusive(self):
        assert when("2026-11-28", "2026-11-30").end == date(2026, 12, 1)
        assert when("2026-11-28", "2026-11-28").end == date(2026, 11, 29)

    @pytest.mark.parametrize(
        ("start", "end", "minutes", "match"),
        [
            ("", None, None, "start is required"),
            ("2026-11-28T15:00", "2026-11-28T14:00", None, "after start"),
            ("2026-11-28T15:00", "2026-11-28T15:00", None, "after start"),
            ("2026-11-28T15:00", "2026-11-29", None, "both be dates"),
            ("2026-11-28", "2026-11-29T10:00", None, "both be dates"),
            ("2026-11-28T15:00", "2026-11-28T16:00", 30, "not both"),
            ("2026-11-28", None, 30, "no duration"),
            ("2026-11-28T15:00", None, 0, "between 1"),
            ("2026-11-28", "2026-11-01", None, "after start"),
            ("2026-01-01", "2028-01-01", None, "at most 366"),
            ("tomorrow", None, None, "ISO"),
        ],
    )
    def test_refusals(self, start, end, minutes, match):
        with pytest.raises(ToolInputError, match=match):
            when(start, end, minutes)

    def test_offsets_are_converted_to_the_server_zone(self):
        assert when("2026-07-01T12:00+00:00").start == datetime(2026, 7, 1, 14, tzinfo=BERLIN)
        assert when("2026-07-01T12:00+00:00").start.utcoffset() == timedelta(hours=2)

    def test_a_spring_forward_gap_time_does_not_crash(self):
        assert when("2027-03-28T02:30").start.astimezone(UTC).year == 2027
