"""Create, update and delete against a real server. NOW is Wed 2026-11-25 15:30 Berlin."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from tests.integration.conftest import count_events, ics, raw, stored, timed, titles


def events_on(agent, cal, day, end=""):
    return agent.call("caldav_get_events", start=day, end=end, calendars=[cal.id])["events"]


class TestCreate:
    def test_a_timed_event_defaults_to_one_hour_and_is_read_back(self, agent, cal):
        answer = agent.call("caldav_create_event", summary="Dentist", start="2026-11-27T09:00", calendar=cal.id)
        event = answer["event"]
        assert answer["created"] is True
        assert (event["start"], event["end"], event["day"]) == (
            "2026-11-27T09:00+01:00",
            "2026-11-27T10:00+01:00",
            "Fri",
        )
        assert event["uid"].endswith("@caldav-mcp") and event["etag"].startswith('"')
        assert titles({"events": events_on(agent, cal, "2026-11-27")}) == ["Dentist"]

    def test_the_stored_event_uses_a_real_zone_and_carries_its_definition(self, agent, cal):
        uid = agent.call("caldav_create_event", summary="Zone check", start="2026-07-01T09:00", calendar=cal.id)[
            "event"
        ]["uid"]
        text = raw(cal, uid)
        assert "TZID=Europe/Berlin:20260701T090000" in text and "BEGIN:VTIMEZONE" in text

    def test_end_and_duration(self, agent, cal):
        a = agent.call(
            "caldav_create_event", summary="A", start="2026-11-27T09:00", end="2026-11-27T11:30", calendar=cal.id
        )
        b = agent.call(
            "caldav_create_event", summary="B", start="2026-11-27T13:00", duration_minutes=45, calendar=cal.id
        )
        assert a["event"]["end"] == "2026-11-27T11:30+01:00" and b["event"]["end"] == "2026-11-27T13:45+01:00"

    def test_a_date_makes_an_all_day_event_and_a_date_end_is_its_last_day(self, agent, cal):
        one = agent.call("caldav_create_event", summary="Holiday", start="2026-12-24", calendar=cal.id)["event"]
        trip = agent.call("caldav_create_event", summary="Trip", start="2026-12-27", end="2026-12-30", calendar=cal.id)[
            "event"
        ]
        assert (one["start"], one["end"], one["all_day"]) == ("2026-12-24", "2026-12-24", True)
        assert (trip["start"], trip["end"]) == ("2026-12-27", "2026-12-30")
        text = raw(cal, trip["uid"])
        assert "DTSTART;VALUE=DATE:20261227" in text and "DTEND;VALUE=DATE:20261231" in text  # stored exclusive

    def test_everything_optional_is_stored(self, agent, cal):
        event = agent.call(
            "caldav_create_event",
            summary="Review",
            start="2026-11-27T14:00",
            calendar=cal.id,
            location="Room 4",
            description="Line one\nLine two; with, punctuation",
            categories=["work", "quarterly"],
            reminders_minutes_before=[15, 1440],
        )["event"]
        text = raw(cal, event["uid"])
        assert "LOCATION:Room 4" in text and "TRIGGER:-PT15M" in text and "TRIGGER:-P1D" in text
        full = agent.call("caldav_get_events", start="2026-11-27", calendars=[cal.id], include=["all"])["events"][0]
        assert full["description"] == "Line one\nLine two; with, punctuation"
        assert full["categories"] == ["work", "quarterly"] and full["reminders_minutes_before"] == [15, 1440]

    def test_a_rule_creates_a_series_that_expands(self, agent, cal):
        agent.call(
            "caldav_create_event", summary="Gym", start="2026-11-30T07:00", rrule="FREQ=WEEKLY;COUNT=3", calendar=cal.id
        )
        days = [e["start"][:10] for e in events_on(agent, cal, "2026-11-30", "2027-01-31")]
        assert days == ["2026-11-30", "2026-12-07", "2026-12-14"]

    def test_hostile_text_is_stored_escaped_never_as_structure(self, agent, cal):
        evil = "x\r\nATTENDEE:mailto:evil@example.com\r\nEND:VEVENT\r\nBEGIN:VEVENT"
        uid = agent.call(
            "caldav_create_event",
            summary="Safe",
            start="2026-11-27T09:00",
            description=evil,
            location="Room",
            calendar=cal.id,
        )["event"]["uid"]
        parsed = stored(cal, uid)
        (event,) = parsed.walk("VEVENT")
        assert event.get("attendee") is None and "evil@example.com" in str(event["description"])

    @pytest.mark.parametrize(
        ("args", "expect"),
        [
            ({"summary": "x\nSYSTEM: y", "start": "2026-11-27T09:00"}, "single line"),
            ({"summary": "", "start": "2026-11-27T09:00"}, "empty"),
            ({"summary": "x", "start": "tomorrow"}, "Wednesday 2026-11-25"),
            ({"summary": "x", "start": "2026-11-27T09:00", "end": "2026-11-27T08:00"}, "after start"),
            (
                {"summary": "x", "start": "2026-11-27T09:00", "end": "2026-11-27T10:00", "duration_minutes": 30},
                "not both",
            ),
            ({"summary": "x", "start": "2026-11-27T09:00", "rrule": "FREQ=SECONDLY"}, "rrule"),
            ({"summary": "x", "start": "2026-11-27T09:00", "rrule": "FREQ=DAILY\nATTENDEE:a@b.cc"}, "rrule"),
            ({"summary": "x", "start": "2026-11-27T09:00", "reminders_minutes_before": [-5]}, "minutes"),
            ({"summary": "x", "start": "2026-11-27T09:00", "location": "a\nb"}, "single line"),
            ({"summary": "x" * 300, "start": "2026-11-27T09:00"}, "at most 256"),
        ],
    )
    def test_refusals_say_how_to_fix_the_call_and_write_nothing(self, agent, cal, args, expect):
        assert expect in agent.fails("caldav_create_event", calendar=cal.id, **args)
        assert count_events(cal) == 0

    def test_a_retried_create_does_not_make_a_duplicate(self, agent, cal):
        first = agent.call("caldav_create_event", summary="Dentist", start="2026-11-27T09:00", calendar=cal.id)
        again = agent.call("caldav_create_event", summary="dentist", start="2026-11-27T09:00", calendar=cal.id)
        assert first["created"] is True and again["created"] is False
        assert again["event"]["uid"] == first["event"]["uid"] and count_events(cal) == 1

    def test_overlaps_are_reported_but_never_block(self, agent, cal):
        cal.save_event(ics(timed("a", "Existing", "20261127T090000", "20261127T100000")))
        cal.save_event(ics(timed("free", "Reminder", "20261127T090000", "20261127T100000", "TRANSP:TRANSPARENT")))
        answer = agent.call("caldav_create_event", summary="New", start="2026-11-27T09:30", calendar=cal.id)
        assert answer["created"] is True
        assert [c["summary"] for c in answer["conflicts"]] == ["Existing"]
        assert any("Overlaps 1" in n for n in answer["notes"])

    def test_back_to_back_is_not_a_conflict_and_the_check_can_be_skipped(self, agent, cal):
        cal.save_event(ics(timed("a", "Existing", "20261127T090000", "20261127T100000")))
        assert "conflicts" not in agent.call(
            "caldav_create_event", summary="Next", start="2026-11-27T10:00", calendar=cal.id
        )
        skipped = agent.call(
            "caldav_create_event", summary="Over", start="2026-11-27T09:30", calendar=cal.id, check_conflicts=False
        )
        assert "conflicts" not in skipped

    def test_conflicts_are_found_in_other_calendars_too(self, agent, cal, make_calendar):
        other = make_calendar()
        other.save_event(ics(timed("a", "Elsewhere", "20261127T090000", "20261127T100000")))
        answer = agent.call("caldav_create_event", summary="New", start="2026-11-27T09:30", calendar=cal.id)
        assert [c["summary"] for c in answer["conflicts"]] == ["Elsewhere"]

    def test_all_day_events_do_not_run_the_conflict_check(self, agent, cal):
        answer = agent.call("caldav_create_event", summary="Holiday", start="2026-12-24", calendar=cal.id)
        assert "conflicts" not in answer and any("all-day" in n for n in answer["notes"])


class TestCalendarChoice:
    def test_with_several_calendars_and_no_default_it_asks(self, agent, cal, make_calendar):
        make_calendar()
        message = agent.fails("caldav_create_event", summary="x", start="2026-11-27T09:00")
        assert "Which calendar" in message and cal.id in message

    def test_the_configured_default_is_used(self, config, cal, make_calendar):
        from dataclasses import replace  # noqa: PLC0415

        from caldav_mcp.app import create_server  # noqa: PLC0415
        from tests.integration.conftest import NOW, Agent  # noqa: PLC0415

        make_calendar()
        agent = Agent(create_server(replace(config, default_calendar=cal.id), clock=lambda: NOW))
        assert agent.call("caldav_create_event", summary="x", start="2026-11-27T09:00")["created"] is True
        assert count_events(cal) == 1

    def test_names_are_case_insensitive(self, agent, make_calendar):
        named = make_calendar("Private Stuff")
        agent.call("caldav_create_event", summary="x", start="2026-11-27T09:00", calendar="private STUFF")
        assert count_events(named) == 1


class TestUpdate:
    @pytest.fixture
    def rich(self, cal):
        cal.save_event(
            ics(
                timed(
                    "rich",
                    "Planning",
                    "20261127T100000",
                    "20261127T110000",
                    "LOCATION:Room 1",
                    "DESCRIPTION:Agenda",
                    'ATTENDEE;CN="Anna":mailto:anna@example.com',
                    "X-CUSTOM-FIELD:keep-me",
                    "CATEGORIES:work",
                )
            )
        )
        return cal

    def test_only_the_given_field_changes_and_nothing_else_is_lost(self, agent, rich):
        agent.call("caldav_update_event", uid="rich", summary="Sprint planning")
        text = raw(rich, "rich")
        assert "SUMMARY:Sprint planning" in text
        for kept in (
            "LOCATION:Room 1",
            "DESCRIPTION:Agenda",
            "anna@example.com",
            "X-CUSTOM-FIELD:keep-me",
            "CATEGORIES:work",
        ):
            assert kept in text

    def test_moving_the_start_keeps_the_length(self, agent, rich):
        event = agent.call("caldav_update_event", uid="rich", start="2026-11-28T15:00")["event"]
        assert (event["start"], event["end"]) == ("2026-11-28T15:00+01:00", "2026-11-28T16:00+01:00")

    def test_changing_only_the_end_keeps_the_start(self, agent, rich):
        event = agent.call("caldav_update_event", uid="rich", end="2026-11-27T12:30")["event"]
        assert (event["start"], event["end"]) == ("2026-11-27T10:00+01:00", "2026-11-27T12:30+01:00")

    def test_start_and_end_and_duration(self, agent, rich):
        both = agent.call("caldav_update_event", uid="rich", start="2026-11-27T08:00", end="2026-11-27T08:45")["event"]
        assert (both["start"], both["end"]) == ("2026-11-27T08:00+01:00", "2026-11-27T08:45+01:00")
        sized = agent.call("caldav_update_event", uid="rich", duration_minutes=90)["event"]
        assert sized["end"] == "2026-11-27T09:30+01:00"

    def test_a_timed_event_can_become_all_day_and_back(self, agent, rich):
        day = agent.call("caldav_update_event", uid="rich", start="2026-11-27")["event"]
        assert day["all_day"] is True and (day["start"], day["end"]) == ("2026-11-27", "2026-11-27")
        timed_again = agent.call("caldav_update_event", uid="rich", start="2026-11-27T10:00")["event"]
        assert "all_day" not in timed_again and timed_again["end"] == "2026-11-27T11:00+01:00"

    def test_clear_removes_a_field_and_an_empty_string_is_refused(self, agent, rich):
        agent.call("caldav_update_event", uid="rich", clear=["location", "description"])
        text = raw(rich, "rich")
        assert "LOCATION" not in text and "DESCRIPTION" not in text and "SUMMARY:Planning" in text
        assert "clear" in agent.fails("caldav_update_event", uid="rich", location="")

    def test_a_field_cannot_be_set_and_cleared_at_once(self, agent, rich):
        assert "both set and listed" in agent.fails("caldav_update_event", uid="rich", location="x", clear=["location"])

    def test_nothing_to_change_is_an_error_not_a_silent_success(self, agent, rich):
        assert "Nothing to change" in agent.fails("caldav_update_event", uid="rich")

    def test_reminders_are_replaced(self, agent, rich):
        agent.call("caldav_update_event", uid="rich", reminders_minutes_before=[30])
        assert raw(rich, "rich").count("BEGIN:VALARM") == 1 and "TRIGGER:-PT30M" in raw(rich, "rich")
        agent.call("caldav_update_event", uid="rich", clear=["reminders"])
        assert "VALARM" not in raw(rich, "rich")

    def test_unknown_uid_is_not_found_and_nothing_is_created(self, agent, rich):
        assert "No event with that UID" in agent.fails("caldav_update_event", uid="ghost", summary="x")
        assert count_events(rich) == 1

    def test_scope_instance_on_a_one_off_event_is_refused(self, agent, rich):
        assert "does not repeat" in agent.fails(
            "caldav_update_event", uid="rich", scope="instance", instance="2026-11-27", summary="x"
        )

    def test_a_stale_etag_refuses_the_write_and_changes_nothing(self, agent, rich):
        stale = agent.call("caldav_get_event_by_uid", uid="rich")["event"]["etag"]
        agent.call("caldav_update_event", uid="rich", summary="Changed elsewhere")
        message = agent.fails("caldav_update_event", uid="rich", summary="Mine", etag=stale)
        assert "changed on the server" in message
        assert "SUMMARY:Changed elsewhere" in raw(rich, "rich")

    def test_the_current_etag_is_accepted(self, agent, rich):
        current = agent.call("caldav_get_event_by_uid", uid="rich")["event"]["etag"]
        assert agent.call("caldav_update_event", uid="rich", summary="Mine", etag=current)["updated"] is True

    def test_a_change_between_the_read_and_the_write_is_refused_by_the_server(self, server, config, rich):
        """The If-Match from the read in this very call protects against a concurrent edit."""
        from caldav_mcp import ops  # noqa: PLC0415
        from caldav_mcp.series import Edits  # noqa: PLC0415
        from caldav_mcp.session import Session  # noqa: PLC0415
        from caldav_mcp.tools.common import App  # noqa: PLC0415
        from tests.integration.conftest import NOW  # noqa: PLC0415

        app = App(config=config, session=Session(config), clock=lambda: NOW)
        found = ops.locate(app, "rich", None)  # reads the event and its ETag
        other = rich.get_event_by_uid("rich")  # somebody else edits it in the meantime
        with other.edit_icalendar_instance() as editing:
            editing.walk("VEVENT")[0]["summary"] = "Somebody else"
        other.save(increase_seqno=False)
        with pytest.raises(ToolError, match="changed on the server"):
            from caldav_mcp.errors import to_tool_error  # noqa: PLC0415

            try:
                ops.update(app, found, scope=None, instance=None, etag=None, edits=Edits(summary="Mine"))
            except Exception as exc:
                raise to_tool_error(exc, "t") from exc
        assert "SUMMARY:Somebody else" in raw(rich, "rich")

    def test_a_server_without_etags_gets_an_unconditional_write_and_the_answer_says_so(self, config, rich):
        from caldav_mcp.ops import Located, check_etag  # noqa: PLC0415
        from caldav_mcp.session import CalendarInfo  # noqa: PLC0415

        class NoEtag:
            etag = None

        found = Located(info=CalendarInfo("c", "c", "http://x/c/", ("VEVENT",)), resource=NoEtag())
        notes = check_etag(found, "supplied")
        assert "unconditional" in notes[0] and "could not be checked" in notes[0]


class TestRecurringUpdate:
    @pytest.fixture
    def series(self, cal):
        cal.save_event(
            ics(
                timed(
                    "s1",
                    "Standup",
                    "20261102T100000",
                    "20261102T103000",
                    "RRULE:FREQ=WEEKLY;COUNT=10",
                    "EXDATE;TZID=Europe/Berlin:20261116T100000",
                ),
                timed(
                    "s1",
                    "Standup (moved)",
                    "20261123T150000",
                    "20261123T153000",
                    "RECURRENCE-ID;TZID=Europe/Berlin:20261123T100000",
                ),
            )
        )
        return cal

    def listing(self, agent, cal, first="2026-11-01", last="2027-01-31"):
        return [f"{e['start'][:16]} {e['summary']}" for e in events_on(agent, cal, first, last)]

    def test_a_repeating_event_needs_a_scope_and_nothing_is_written_without_one(self, agent, series):
        before = raw(series, "s1")
        message = agent.fails("caldav_update_event", uid="s1", summary="x")
        assert "FREQ=WEEKLY;COUNT=10" in message and "scope='series'" in message and "this_and_future" in message
        assert raw(series, "s1") == before

    def test_renaming_the_series_keeps_the_moved_and_the_cancelled_occurrence(self, agent, series):
        """The upstream bug: this used to destroy the exception at 23 Nov."""
        agent.call("caldav_update_event", uid="s1", scope="series", summary="Daily sync")
        listing = self.listing(agent, series)
        assert "2026-11-23T15:00 Standup (moved)" in listing
        assert not [item for item in listing if item.startswith("2026-11-16")]
        assert listing.count("2026-11-30T10:00 Daily sync") == 1
        assert raw(series, "s1").count("BEGIN:VEVENT") == 2

    def test_moving_the_series_carries_its_exceptions_along(self, agent, series):
        agent.call("caldav_update_event", uid="s1", scope="series", start="2026-11-02T11:00")
        listing = self.listing(agent, series)
        assert "2026-11-23T15:00 Standup (moved)" in listing
        assert not [item for item in listing if item.startswith("2026-11-16")]
        assert "2026-11-30T11:00 Standup" in listing and "2026-11-30T10:00 Standup" not in listing

    def test_one_occurrence_can_be_moved_without_touching_the_rest(self, agent, series):
        answer = agent.call(
            "caldav_update_event", uid="s1", scope="instance", instance="2026-11-30T10:00", start="2026-11-30T16:00"
        )
        assert answer["event"]["start"] == "2026-11-30T16:00+01:00" and answer["scope"] == "instance"
        listing = self.listing(agent, series)
        assert "2026-11-30T16:00 Standup" in listing and "2026-11-30T10:00 Standup" not in listing
        assert "2026-12-07T10:00 Standup" in listing and "2026-11-23T15:00 Standup (moved)" in listing

    def test_an_occurrence_can_be_named_by_its_date(self, agent, series):
        agent.call("caldav_update_event", uid="s1", scope="instance", instance="2026-12-07", location="Room 9")
        assert (
            agent.call("caldav_get_events", start="2026-12-07", calendars=[series.id])["events"][0]["location"]
            == "Room 9"
        )

    def test_a_wrong_instance_is_refused_with_the_real_ones_listed(self, agent, series):
        message = agent.fails(
            "caldav_update_event", uid="s1", scope="instance", instance="2026-11-24T10:00", summary="x"
        )
        assert "No occurrence" in message and "2026-11-23T10:00" in message and "2026-11-30T10:00" in message

    def test_the_instance_is_required_for_instance_and_future_scopes(self, agent, series):
        assert "needs `instance`" in agent.fails("caldav_update_event", uid="s1", scope="instance", summary="x")
        assert "needs `instance`" in agent.fails("caldav_update_event", uid="s1", scope="this_and_future", summary="x")

    def test_this_and_future_splits_into_two_series_and_loses_nothing(self, agent, series):
        before = self.listing(agent, series)
        answer = agent.call(
            "caldav_update_event", uid="s1", scope="this_and_future", instance="2026-11-30T10:00", location="New room"
        )
        new_uid = answer["event"]["uid"]
        assert new_uid != "s1" and answer["scope"] == "this_and_future"
        after = self.listing(agent, series)
        assert after == before
        old_rule = stored(series, "s1").walk("VEVENT")[0]["rrule"].to_ical().decode()
        assert "UNTIL=20261130T085959Z" in old_rule and "COUNT" not in old_rule
        located = {e["start"][:10]: e.get("location") for e in events_on(agent, series, "2026-11-01", "2027-01-31")}
        assert (
            located["2026-11-23"] is None
            and located["2026-11-30"] == "New room"
            and located["2027-01-04"] == "New room"
        )

    def test_this_and_future_from_the_first_occurrence_is_just_the_series(self, agent, series):
        answer = agent.call(
            "caldav_update_event", uid="s1", scope="this_and_future", instance="2026-11-02T10:00", summary="All"
        )
        assert answer["event"]["uid"] == "s1" and any("first occurrence" in n for n in answer["notes"])

    def test_this_and_future_with_a_new_time_moves_the_exceptions_in_the_future(self, agent, series):
        agent.call(
            "caldav_update_event",
            uid="s1",
            scope="this_and_future",
            instance="2026-11-23T10:00",
            start="2026-11-23T12:00",
        )
        listing = self.listing(agent, series)
        assert "2026-11-23T15:00 Standup (moved)" in listing and "2026-11-30T12:00 Standup" in listing
        assert "2026-11-09T10:00 Standup" in listing and "2026-11-30T10:00 Standup" not in listing


class TestDelete:
    def test_a_one_off_event_is_deleted_and_the_answer_says_which(self, agent, cal):
        cal.save_event(ics(timed("d1", "Dentist", "20261127T090000", "20261127T100000")))
        answer = agent.call("caldav_delete_event", uid="d1")
        assert (
            answer["deleted"] is True
            and answer["event"]["summary"] == "Dentist"
            and answer["event"]["start"].startswith("2026-11-27T09:00")
        )
        assert count_events(cal) == 0
        assert "No event with that UID" in agent.fails("caldav_get_event_by_uid", uid="d1", calendar=cal.id)

    def test_unknown_uid_deletes_nothing(self, agent, cal):
        cal.save_event(ics(timed("d1", "Dentist", "20261127T090000", "20261127T100000")))
        assert "No event with that UID" in agent.fails("caldav_delete_event", uid="ghost")
        assert count_events(cal) == 1

    def test_a_stale_etag_refuses_and_the_event_stays(self, agent, cal):
        cal.save_event(ics(timed("d1", "Dentist", "20261127T090000", "20261127T100000")))
        stale = agent.call("caldav_get_event_by_uid", uid="d1")["event"]["etag"]
        agent.call("caldav_update_event", uid="d1", summary="Changed")
        assert "changed on the server" in agent.fails("caldav_delete_event", uid="d1", etag=stale)
        assert count_events(cal) == 1

    @pytest.fixture
    def series(self, cal):
        cal.save_event(
            ics(
                timed("s1", "Standup", "20261102T100000", "20261102T103000", "RRULE:FREQ=WEEKLY;COUNT=10"),
                timed(
                    "s1",
                    "Standup (moved)",
                    "20261123T150000",
                    "20261123T153000",
                    "RECURRENCE-ID;TZID=Europe/Berlin:20261123T100000",
                ),
            )
        )
        return cal

    def starts(self, agent, cal):
        return [e["start"][:16] for e in events_on(agent, cal, "2026-11-01", "2027-01-31")]

    def test_deleting_a_repeating_event_without_a_scope_deletes_nothing(self, agent, series):
        message = agent.fails("caldav_delete_event", uid="s1")
        assert "scope='series'" in message and "instance" in message
        assert len(self.starts(agent, series)) == 10

    def test_cancelling_one_occurrence_leaves_the_rest_of_the_series(self, agent, series):
        """'Cancel next Tuesday's standup' must not delete every standup."""
        answer = agent.call("caldav_delete_event", uid="s1", scope="instance", instance="2026-11-30T10:00")
        assert answer["event"]["recurrence_id"] == "2026-11-30T10:00+01:00"
        starts = self.starts(agent, series)
        assert len(starts) == 9 and "2026-11-30T10:00" not in starts and "2026-12-07T10:00" in starts

    def test_the_answer_describes_the_occurrence_removed_not_the_first_one(self, agent, series):
        plain = agent.call("caldav_delete_event", uid="s1", scope="instance", instance="2026-11-30T10:00")["event"]
        assert (plain["start"], plain["end"]) == ("2026-11-30T10:00+01:00", "2026-11-30T10:30+01:00")
        moved = agent.call("caldav_delete_event", uid="s1", scope="instance", instance="2026-11-23T10:00")["event"]
        assert (moved["start"], moved["end"], moved["summary"]) == (
            "2026-11-23T15:00+01:00",
            "2026-11-23T15:30+01:00",
            "Standup (moved)",
        )

    def test_cancelling_a_moved_occurrence_removes_the_override_too(self, agent, series):
        agent.call("caldav_delete_event", uid="s1", scope="instance", instance="2026-11-23T10:00")
        starts = self.starts(agent, series)
        assert not [s for s in starts if s.startswith("2026-11-23")] and len(starts) == 9
        assert raw(series, "s1").count("BEGIN:VEVENT") == 1

    def test_this_and_future_ends_the_series_before_the_occurrence(self, agent, series):
        agent.call("caldav_delete_event", uid="s1", scope="this_and_future", instance="2026-11-30T10:00")
        starts = self.starts(agent, series)
        assert starts == ["2026-11-02T10:00", "2026-11-09T10:00", "2026-11-16T10:00", "2026-11-23T15:00"]

    def test_this_and_future_from_the_first_occurrence_deletes_the_series(self, agent, series):
        answer = agent.call("caldav_delete_event", uid="s1", scope="this_and_future", instance="2026-11-02T10:00")
        assert any("first occurrence" in n for n in answer["notes"]) and count_events(series) == 0

    def test_deleting_the_series_removes_every_occurrence_and_says_so(self, agent, series):
        answer = agent.call("caldav_delete_event", uid="s1", scope="series")
        assert answer["scope"] == "series" and count_events(series) == 0 and self.starts(agent, series) == []


def test_read_after_write_matches_what_an_agent_would_ask_next(agent, cal):
    created = agent.call(
        "caldav_create_event", summary="Lunch with Anna", start="2026-11-27T12:00", location="Cafe", calendar=cal.id
    )
    found = agent.call("caldav_search_events", query="anna", calendars=[cal.id])
    assert [e["uid"] for e in found["events"]] == [created["event"]["uid"]]
