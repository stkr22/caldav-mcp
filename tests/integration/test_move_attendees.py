"""Move and attendee tools against a real server."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.integration.conftest import count_events, ics, raw, stored, timed


@pytest.fixture
def two(cal, make_calendar):
    cal.save_event(ics(timed("m1", "Movable", "20261127T090000", "20261127T100000", "LOCATION:Here")))
    return cal, make_calendar()


class TestMove:
    def test_moves_keep_the_uid_and_use_the_servers_own_move(self, agent, two):
        source, target = two
        answer = agent.call("caldav_move_event", uid="m1", target_calendar=target.id)
        assert answer["moved"] is True and answer["method"] == "MOVE" and answer["event"]["uid"] == "m1"
        assert count_events(source) == 0 and count_events(target) == 1
        assert "LOCATION:Here" in raw(target, "m1")

    def test_a_whole_series_with_its_exceptions_moves_together(self, agent, cal, make_calendar):
        target = make_calendar()
        cal.save_event(
            ics(
                timed("s1", "Standup", "20261102T100000", "20261102T103000", "RRULE:FREQ=WEEKLY;COUNT=5"),
                timed(
                    "s1",
                    "Moved one",
                    "20261109T150000",
                    "20261109T153000",
                    "RECURRENCE-ID;TZID=Europe/Berlin:20261109T100000",
                ),
            )
        )
        agent.call("caldav_move_event", uid="s1", target_calendar=target.id)
        assert raw(target, "s1").count("BEGIN:VEVENT") == 2 and count_events(cal) == 0

    def test_same_calendar_is_refused(self, agent, two):
        source, _ = two
        assert "already in that calendar" in agent.fails("caldav_move_event", uid="m1", target_calendar=source.id)

    def test_unknown_target_lists_the_calendars(self, agent, two):
        source, _ = two
        message = agent.fails("caldav_move_event", uid="m1", target_calendar="nowhere")
        assert "nowhere" in message and source.id in message and count_events(source) == 1

    def test_a_uid_already_in_the_target_is_refused_and_nothing_moves(self, agent, two):
        source, target = two
        target.save_event(ics(timed("m1", "Same uid", "20261201T090000", "20261201T100000")))
        assert "already exists in the target" in agent.fails(
            "caldav_move_event", uid="m1", source_calendar=source.id, target_calendar=target.id
        )
        assert count_events(source) == 1 and count_events(target) == 1

    def test_unknown_uid(self, agent, two):
        _, target = two
        assert "No event with that UID" in agent.fails("caldav_move_event", uid="ghost", target_calendar=target.id)

    def test_a_server_without_move_falls_back_to_copy_then_delete(self, agent, two, dav, monkeypatch):
        source, target = two
        original = type(dav).request

        def no_move(self, url, method="GET", *args, **kwargs):
            if method == "MOVE":
                return SimpleNamespace(status=405)
            return original(self, url, method, *args, **kwargs)

        monkeypatch.setattr(type(dav), "request", no_move)
        answer = agent.call("caldav_move_event", uid="m1", target_calendar=target.id)
        assert answer["method"].startswith("copy then delete")
        assert count_events(source) == 0 and "LOCATION:Here" in raw(target, "m1")

    def test_if_the_delete_fails_after_copying_the_copy_is_removed_again(self, agent, two, dav, monkeypatch):
        source, target = two
        original = type(dav).request
        refused: list[str] = []

        def refuse(self, url, method="GET", *args, **kwargs):
            if method == "MOVE":
                return SimpleNamespace(status=405)
            if method == "DELETE" and source.id in str(url):
                refused.append(str(url))
                raise RuntimeError("the server refused the delete")
            return original(self, url, method, *args, **kwargs)

        monkeypatch.setattr(type(dav), "request", refuse)
        message = agent.fails("caldav_move_event", uid="m1", target_calendar=target.id)
        monkeypatch.undo()
        assert refused, "the injected failure was never reached, so this test proved nothing"
        assert "The copy was removed again, so nothing changed" in message
        assert count_events(source) == 1 and count_events(target) == 0


class TestAttendees:
    @pytest.fixture
    def meeting(self, cal):
        cal.save_event(
            ics(
                timed(
                    "a1", "Meeting", "20261127T090000", "20261127T100000", 'ATTENDEE;CN="Anna":mailto:anna@example.com'
                )
            )
        )
        return cal

    def emails(self, cal, uid="a1"):
        event = stored(cal, uid).walk("VEVENT")[0]
        people = event.get("attendee")
        people = people if isinstance(people, list) else [people] if people is not None else []
        return sorted(str(p).removeprefix("mailto:") for p in people)

    def test_add_stores_a_well_formed_attendee(self, agent, meeting):
        answer = agent.call("caldav_add_attendee", uid="a1", email="Bob@Example.com", name="Bob B")
        assert answer["added"] == "bob@example.com"
        assert {a["email"] for a in answer["attendees"]} == {"anna@example.com", "bob@example.com"}
        text = raw(meeting, "a1")
        assert 'CN="Bob B"' in text or "CN=Bob B" in text
        assert "PARTSTAT=NEEDS-ACTION" in text and "RSVP=TRUE" in text and "ROLE=REQ-PARTICIPANT" in text

    def test_adding_an_existing_attendee_changes_nothing_and_says_so(self, agent, meeting):
        answer = agent.call("caldav_add_attendee", uid="a1", email="ANNA@example.com")
        assert any("already an attendee" in n for n in answer["notes"]) and self.emails(meeting) == ["anna@example.com"]

    def test_remove(self, agent, meeting):
        agent.call("caldav_add_attendee", uid="a1", email="bob@example.com")
        answer = agent.call("caldav_remove_attendee", uid="a1", email="anna@example.com")
        assert [a["email"] for a in answer["attendees"]] == ["bob@example.com"] and self.emails(meeting) == [
            "bob@example.com"
        ]

    def test_removing_someone_who_is_not_there_lists_who_is(self, agent, meeting):
        message = agent.fails("caldav_remove_attendee", uid="a1", email="zed@example.com")
        assert "anna@example.com" in message and self.emails(meeting) == ["anna@example.com"]

    @pytest.mark.parametrize(
        "email", ["", "not-an-email", "a@b.com\nATTENDEE:mailto:evil@example.com", "a@b.com,c@d.com", "<a@b.com>"]
    )
    def test_bad_addresses_are_refused_and_nothing_is_written(self, agent, meeting, email):
        assert "email" in agent.fails("caldav_add_attendee", uid="a1", email=email).lower()
        assert self.emails(meeting) == ["anna@example.com"]

    def test_a_bad_role_is_refused(self, agent, meeting):
        assert "role must be one of" in agent.fails("caldav_add_attendee", uid="a1", email="b@example.com", role="BOSS")

    def test_a_repeating_event_needs_scope_series(self, agent, cal):
        cal.save_event(ics(timed("r1", "Standup", "20261102T100000", "20261102T103000", "RRULE:FREQ=WEEKLY;COUNT=3")))
        assert "scope='series'" in agent.fails("caldav_add_attendee", uid="r1", email="b@example.com")
        assert (
            agent.call("caldav_add_attendee", uid="r1", email="b@example.com", scope="series")["added"]
            == "b@example.com"
        )

    def test_a_stale_etag_is_refused(self, agent, meeting):
        stale = agent.call("caldav_get_event_by_uid", uid="a1")["event"]["etag"]
        agent.call("caldav_update_event", uid="a1", summary="Changed")
        assert "changed on the server" in agent.fails(
            "caldav_add_attendee", uid="a1", email="b@example.com", etag=stale
        )
        assert self.emails(meeting) == ["anna@example.com"]


class TestToolSurface:
    def names(self, config, cal, **changes):
        import asyncio  # noqa: PLC0415
        from dataclasses import replace  # noqa: PLC0415

        from fastmcp import Client  # noqa: PLC0415

        from caldav_mcp.app import create_server  # noqa: PLC0415

        async def run():
            async with Client(create_server(replace(config, **changes))) as client:
                return {t.name for t in await client.list_tools()}

        return asyncio.run(run())

    def test_all_eleven_tools_are_registered_by_default(self, config, cal):
        assert len(self.names(config, cal)) == 11

    def test_read_only_registers_only_the_five_read_tools(self, config, cal):
        assert self.names(config, cal, read_only=True) == {
            "caldav_get_event_by_uid",
            "caldav_get_events",
            "caldav_get_freebusy",
            "caldav_list_calendars",
            "caldav_search_events",
        }

    def test_the_deployment_allow_list_gives_exactly_eight_tools(self, config, cal):
        allow = "caldav_list_calendars,caldav_get_events,caldav_search_events,caldav_get_event_by_uid,caldav_get_freebusy,caldav_create_event,caldav_update_event,caldav_delete_event"
        names = self.names(config, cal, allow_tools=allow)
        assert len(names) == 8 and not names & {"caldav_move_event", "caldav_add_attendee", "caldav_remove_attendee"}

    def test_move_and_attendee_tools_can_each_be_denied_alone(self, config, cal):
        for denied in ("caldav_move_event", "caldav_add_attendee", "caldav_remove_attendee"):
            names = self.names(config, cal, deny_tools=denied)
            assert denied not in names and len(names) == 10
