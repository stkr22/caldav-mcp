"""Recurrence scopes as pure functions: nothing is lost, nothing is duplicated, exceptions follow their dates."""

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import recurring_ical_events
from icalendar import Calendar

from caldav_mcp import series
from caldav_mcp.errors import ToolInputError
from caldav_mcp.series import (
    Edits,
    edit_instance,
    edit_series,
    exclude_instance,
    is_recurring,
    resolve_instance,
    split_future,
)

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 25, 15, 30, tzinfo=BERLIN)


def split(cal: Calendar) -> tuple[Any, list[Any]]:
    """The master and overrides, typed loosely: icalendar property values are unions no checker can index."""
    return series.split(cal)


def events_of(cal: Calendar) -> list[Any]:
    return list(series.events_of(cal))


def parse(*components: str) -> Calendar:
    body = "".join(f"BEGIN:VEVENT\r\n{c}\r\nEND:VEVENT\r\n" for c in components)
    return Calendar.from_ical(f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n{body}END:VCALENDAR\r\n")


def weekly(*extra: str, rule: str = "FREQ=WEEKLY;COUNT=10") -> str:
    return "\r\n".join(
        [
            "UID:s1",
            "DTSTAMP:20260101T000000Z",
            "SUMMARY:Standup",
            "DTSTART;TZID=Europe/Berlin:20261102T100000",
            "DTEND;TZID=Europe/Berlin:20261102T103000",
            f"RRULE:{rule}",
            *extra,
        ]
    )


def moved_override() -> str:
    return "\r\n".join(
        [
            "UID:s1",
            "DTSTAMP:20260101T000000Z",
            "SUMMARY:Standup (moved)",
            "RECURRENCE-ID;TZID=Europe/Berlin:20261123T100000",
            "DTSTART;TZID=Europe/Berlin:20261123T150000",
            "DTEND;TZID=Europe/Berlin:20261123T153000",
        ]
    )


def starts(cal: Calendar, first: str = "2026-10-01", last: str = "2027-03-01") -> list[str]:
    found = recurring_ical_events.of(cal).between(date.fromisoformat(first), date.fromisoformat(last))
    return sorted(f"{c.start.astimezone(BERLIN):%Y-%m-%d %H:%M} {c.get('summary')}" for c in found)


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=BERLIN)


class TestStructure:
    def test_split_finds_master_and_overrides(self):
        master, overrides = split(parse(weekly(), moved_override()))
        assert master.get("rrule") is not None and len(overrides) == 1

    def test_plain_event_is_not_recurring(self):
        cal = parse("UID:p\r\nDTSTART:20261126T100000Z\r\nDTEND:20261126T110000Z\r\nSUMMARY:x")
        assert not is_recurring(*split(cal))

    def test_overrides_alone_cannot_be_edited(self):
        with pytest.raises(ToolInputError, match="only of modified occurrences"):
            split(parse(moved_override()))


class TestResolveInstance:
    def test_exact_start_of_an_ordinary_occurrence(self):
        assert resolve_instance(parse(weekly()), local(2026, 11, 30, 10), BERLIN) == local(2026, 11, 30, 10)

    def test_the_original_start_of_a_moved_occurrence_is_what_names_it(self):
        cal = parse(weekly(), moved_override())
        assert resolve_instance(cal, local(2026, 11, 23, 10), BERLIN) == local(2026, 11, 23, 10)

    def test_the_moved_start_is_not_the_name_and_the_error_says_what_is(self):
        with pytest.raises(ToolInputError, match="Nearest occurrences") as info:
            resolve_instance(parse(weekly(), moved_override()), local(2026, 11, 23, 15), BERLIN)
        assert "2026-11-23T10:00" in str(info.value)

    def test_a_plain_date_names_the_single_occurrence_that_day(self):
        assert resolve_instance(parse(weekly()), date(2026, 12, 7), BERLIN) == local(2026, 12, 7, 10)

    def test_a_date_with_two_occurrences_is_ambiguous(self):
        cal = parse(weekly(rule="FREQ=DAILY;BYHOUR=9,15;COUNT=20"))
        with pytest.raises(ToolInputError, match="2 occurrences start on 2026-11-03"):
            resolve_instance(cal, date(2026, 11, 3), BERLIN)

    def test_a_date_that_is_not_an_occurrence(self):
        with pytest.raises(ToolInputError, match="No occurrence"):
            resolve_instance(parse(weekly()), date(2026, 12, 8), BERLIN)

    def test_an_excluded_date_is_not_an_occurrence(self):
        cal = parse(weekly("EXDATE;TZID=Europe/Berlin:20261116T100000"))
        with pytest.raises(ToolInputError, match="No occurrence"):
            resolve_instance(cal, local(2026, 11, 16, 10), BERLIN)

    def test_an_offset_spelling_of_the_same_instant_matches(self):
        requested = datetime(2026, 11, 30, 9, 0, tzinfo=UTC)
        assert resolve_instance(parse(weekly()), requested, BERLIN) == local(2026, 11, 30, 10)


class TestSeriesEdit:
    def test_renaming_keeps_every_override(self):
        cal = parse(weekly(), moved_override())
        edit_series(cal, Edits(summary="Daily sync"), BERLIN, NOW)
        master, overrides = split(cal)
        assert str(master["summary"]) == "Daily sync" and len(overrides) == 1
        assert str(overrides[0]["summary"]) == "Standup (moved)"

    def test_moving_the_series_moves_exceptions_with_it(self):
        cal = parse(weekly("EXDATE;TZID=Europe/Berlin:20261116T100000"), moved_override())
        edit_series(cal, Edits(start=local(2026, 11, 2, 11)), BERLIN, NOW)
        master, (override,) = split(cal)
        assert master.start == local(2026, 11, 2, 11) and master.end == local(2026, 11, 2, 11, 30)
        assert override["recurrence-id"].dt == local(2026, 11, 23, 11)
        assert master["exdate"].dts[0].dt == local(2026, 11, 16, 11)
        assert "2026-11-16 11:00" not in "".join(starts(cal))
        assert "2026-11-23 15:00 Standup (moved)" in starts(cal)

    def test_changing_only_the_end_keeps_the_start(self):
        cal = parse(weekly())
        edit_series(cal, Edits(end=local(2026, 11, 2, 12)), BERLIN, NOW)
        master, _ = split(cal)
        assert master.start == local(2026, 11, 2, 10) and master.end == local(2026, 11, 2, 12)

    def test_duration_replaces_the_length(self):
        cal = parse(weekly())
        edit_series(cal, Edits(duration_minutes=90), BERLIN, NOW)
        assert split(cal)[0].end == local(2026, 11, 2, 11, 30)

    def test_clear_removes_fields_and_reminders(self):
        cal = parse(weekly("LOCATION:Room 1", "DESCRIPTION:Notes"))
        edit_series(cal, Edits(reminders=[10]), BERLIN, NOW)
        assert len(split(cal)[0].subcomponents) == 1
        edit_series(cal, Edits(clear=frozenset({"location", "reminders"})), BERLIN, NOW)
        master, _ = split(cal)
        assert master.get("location") is None and master.subcomponents == [] and master.get("description") is not None

    def test_a_missing_end_after_moving_to_all_day_defaults_sensibly(self):
        cal = parse(weekly())
        edit_series(cal, Edits(start=date(2026, 11, 2)), BERLIN, NOW)
        master, _ = split(cal)
        assert master.end == date(2026, 11, 3)

    def test_sequence_is_bumped_only_when_present(self):
        cal = parse(weekly("SEQUENCE:2"))
        edit_series(cal, Edits(summary="x"), BERLIN, NOW)
        assert int(split(cal)[0]["sequence"]) == 3
        plain = parse(weekly())
        edit_series(plain, Edits(summary="x"), BERLIN, NOW)
        assert split(plain)[0].get("sequence") is None

    def test_modification_stamps_are_set_in_utc(self):
        cal = parse(weekly())
        edit_series(cal, Edits(summary="x"), BERLIN, NOW)
        assert split(cal)[0]["last-modified"].dt == NOW.astimezone(UTC)


class TestInstanceEdit:
    def test_editing_an_unmodified_occurrence_creates_one_override_and_leaves_the_rest(self):
        cal = parse(weekly())
        edit_instance(cal, local(2026, 11, 30, 10), Edits(start=local(2026, 11, 30, 16)), BERLIN, NOW)
        assert len(events_of(cal)) == 2
        listed = starts(cal)
        assert "2026-11-30 16:00 Standup" in listed and "2026-11-30 10:00 Standup" not in listed
        assert "2026-12-07 10:00 Standup" in listed and "2026-11-23 10:00 Standup" in listed

    def test_the_override_keeps_the_masters_length_unless_told_otherwise(self):
        cal = parse(weekly())
        edit_instance(cal, local(2026, 11, 30, 10), Edits(summary="Special"), BERLIN, NOW)
        override = events_of(cal)[1]
        assert override.start == local(2026, 11, 30, 10) and override.end == local(2026, 11, 30, 10, 30)
        assert override.get("rrule") is None and str(override["uid"]) == "s1"

    def test_editing_the_same_occurrence_twice_edits_one_override(self):
        cal = parse(weekly())
        edit_instance(cal, local(2026, 11, 30, 10), Edits(summary="One"), BERLIN, NOW)
        edit_instance(cal, local(2026, 11, 30, 10), Edits(location="Room 2"), BERLIN, NOW)
        assert len(events_of(cal)) == 2
        override = events_of(cal)[1]
        assert str(override["summary"]) == "One" and str(override["location"]) == "Room 2"

    def test_an_existing_override_is_edited_in_place(self):
        cal = parse(weekly(), moved_override())
        edit_instance(cal, local(2026, 11, 23, 10), Edits(summary="Again"), BERLIN, NOW)
        assert len(events_of(cal)) == 2 and "2026-11-23 15:00 Again" in starts(cal)

    def test_a_rule_cannot_be_given_to_one_occurrence(self):
        with pytest.raises(ToolInputError, match="single occurrence"):
            edit_instance(parse(weekly()), local(2026, 11, 30, 10), Edits(rrule="FREQ=DAILY"), BERLIN, NOW)

    def test_master_is_untouched_by_an_instance_edit(self):
        cal = parse(weekly())
        edit_instance(cal, local(2026, 11, 30, 10), Edits(summary="Special"), BERLIN, NOW)
        assert str(split(cal)[0]["summary"]) == "Standup"


class TestExclude:
    def test_excluding_adds_an_exdate_and_the_occurrence_disappears(self):
        cal = parse(weekly())
        exclude_instance(cal, local(2026, 11, 30, 10), BERLIN)
        assert "2026-11-30 10:00 Standup" not in starts(cal) and "2026-12-07 10:00 Standup" in starts(cal)

    def test_excluding_a_moved_occurrence_removes_its_override_too(self):
        cal = parse(weekly(), moved_override())
        exclude_instance(cal, local(2026, 11, 23, 10), BERLIN)
        assert len(events_of(cal)) == 1 and not [s for s in starts(cal) if "2026-11-23" in s]

    def test_excluding_twice_does_not_duplicate(self):
        cal = parse(weekly())
        exclude_instance(cal, local(2026, 11, 30, 10), BERLIN)
        exclude_instance(cal, local(2026, 11, 30, 10), BERLIN)
        assert len(split(cal)[0]["exdate"].dts) == 1


class TestSplit:
    """The invariant: occurrences before the split plus occurrences after it are exactly the originals."""

    def occurrences(self, *cals: Calendar) -> list[str]:
        return sorted(o for c in cals for o in starts(c, "2026-10-01", "2027-06-01"))

    def test_count_is_shared_between_the_two_series(self):
        cal = parse(weekly())
        original = self.occurrences(parse(weekly()))
        fresh = split_future(cal, local(2026, 11, 23, 10), Edits(), BERLIN, NOW)
        assert self.occurrences(cal, fresh) == original
        assert len(starts(cal)) == 3 and len(starts(fresh)) == 7
        assert str(split(cal)[0]["rrule"]["UNTIL"][0].isoformat()).startswith("2026-11-23T08:59:59")

    def test_the_new_series_has_a_new_uid_and_the_old_keeps_its_own(self):
        cal = parse(weekly())
        fresh = split_future(cal, local(2026, 11, 23, 10), Edits(), BERLIN, NOW)
        assert str(split(fresh)[0]["uid"]) != "s1" and str(split(cal)[0]["uid"]) == "s1"
        assert str(split(fresh)[0]["uid"]).endswith("@caldav-mcp")

    def test_future_edits_apply_only_to_the_new_series(self):
        cal = parse(weekly())
        fresh = split_future(
            cal, local(2026, 11, 23, 10), Edits(summary="Renamed", start=local(2026, 11, 23, 11)), BERLIN, NOW
        )
        assert all(s.endswith("Standup") for s in starts(cal))
        assert starts(fresh)[0] == "2026-11-23 11:00 Renamed" and starts(fresh)[1] == "2026-11-30 11:00 Renamed"

    def test_unbounded_series_stay_unbounded_and_get_an_until_on_the_old_one(self):
        cal = parse(weekly(rule="FREQ=WEEKLY"))
        fresh = split_future(cal, local(2026, 11, 23, 10), Edits(), BERLIN, NOW)
        assert (
            "UNTIL" in split(cal)[0]["rrule"]
            and "UNTIL" not in split(fresh)[0]["rrule"]
            and "COUNT" not in split(fresh)[0]["rrule"]
        )
        assert starts(cal, "2026-10-01", "2028-01-01")[-1].startswith("2026-11-16")
        assert starts(fresh, "2026-10-01", "2028-01-01")[0].startswith("2026-11-23")

    def test_exceptions_in_the_future_travel_with_the_new_series(self):
        cal = parse(weekly("EXDATE;TZID=Europe/Berlin:20261116T100000,20261207T100000"), moved_override())
        original = self.occurrences(
            parse(weekly("EXDATE;TZID=Europe/Berlin:20261116T100000,20261207T100000"), moved_override())
        )
        fresh = split_future(cal, local(2026, 11, 23, 10), Edits(), BERLIN, NOW)
        assert self.occurrences(cal, fresh) == original
        assert len(events_of(cal)) == 1 and len(events_of(fresh)) == 2
        assert [str(c["uid"]) for c in events_of(fresh)] == [str(split(fresh)[0]["uid"])] * 2
        assert "2026-11-16 10:00 Standup" not in starts(cal)

    def test_moving_the_future_moves_its_exceptions(self):
        cal = parse(weekly(), moved_override())
        fresh = split_future(cal, local(2026, 11, 23, 10), Edits(start=local(2026, 11, 23, 12)), BERLIN, NOW)
        listed = starts(fresh)
        assert "2026-11-23 15:00 Standup (moved)" in listed and "2026-11-30 12:00 Standup" in listed
        assert not [s for s in listed if s.startswith("2026-11-23 12:00")]

    def test_splitting_across_the_winter_time_change_keeps_wall_clock_times(self):
        rule = "FREQ=DAILY;COUNT=8"
        cal = parse(
            weekly(rule=rule)
            .replace("20261102T100000", "20261022T100000")
            .replace("20261102T103000", "20261022T103000")
        )
        before = self.occurrences(
            parse(
                weekly(rule=rule)
                .replace("20261102T100000", "20261022T100000")
                .replace("20261102T103000", "20261022T103000")
            )
        )
        fresh = split_future(cal, local(2026, 10, 26, 10), Edits(), BERLIN, NOW)
        assert self.occurrences(cal, fresh) == before
        assert {s[11:16] for s in before} == {"10:00"}

    def test_all_day_series_split_with_a_date_until(self):
        base = "\r\n".join(
            [
                "UID:a",
                "SUMMARY:Holiday",
                "DTSTART;VALUE=DATE:20261102",
                "DTEND;VALUE=DATE:20261103",
                "RRULE:FREQ=WEEKLY;COUNT=6",
            ]
        )
        cal = parse(base)
        fresh = split_future(cal, date(2026, 11, 16), Edits(), BERLIN, NOW)
        until = split(cal)[0]["rrule"]["UNTIL"][0]
        assert until == date(2026, 11, 15)
        assert split(fresh)[0].start == date(2026, 11, 16) and split(fresh)[0]["rrule"]["COUNT"] == [4]

    def test_floating_series_split_uses_a_floating_until(self):
        base = "\r\n".join(
            ["UID:f", "SUMMARY:Float", "DTSTART:20261102T100000", "DTEND:20261102T110000", "RRULE:FREQ=WEEKLY;COUNT=5"]
        )
        cal = parse(base)
        split_future(cal, datetime(2026, 11, 16, 10), Edits(), BERLIN, NOW)
        until = split(cal)[0]["rrule"]["UNTIL"][0]
        assert until.tzinfo is None and until == datetime(2026, 11, 16, 9, 59, 59)

    def test_a_non_recurring_event_cannot_be_split(self):
        cal = parse("UID:p\r\nDTSTART:20261126T100000Z\r\nDTEND:20261126T110000Z\r\nSUMMARY:x")
        with pytest.raises(ToolInputError, match="does not repeat"):
            split_future(cal, datetime(2026, 11, 26, 10, tzinfo=UTC), Edits(), BERLIN, NOW)


def test_split_by_weekday_rules_keeps_the_pattern():
    rule = "FREQ=WEEKLY;BYDAY=MO,WE;COUNT=12"
    text = weekly(rule=rule)
    cal, original = parse(text), starts(parse(text), "2026-10-01", "2027-06-01")
    fresh = split_future(cal, local(2026, 11, 18, 10), Edits(), BERLIN, NOW)
    assert sorted(starts(cal, "2026-10-01", "2027-06-01") + starts(fresh, "2026-10-01", "2027-06-01")) == original
