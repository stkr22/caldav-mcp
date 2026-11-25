"""The capability probe reports what the server can do and nothing about what is in it."""

from __future__ import annotations

import json

import pytest

from caldav_mcp.probe import main, render, run
from tests.integration.conftest import BASE, PASSWORD, USER, ics, timed

SECRETS = ("TOPSECRET-TITLE", "TOPSECRET-PLACE", "topsecret@example.com", "TOPSECRET-NOTES", "TOPSECRET-CAL")


@pytest.fixture
def populated(make_calendar):
    cal = make_calendar("TOPSECRET-CAL")
    start = "20261201T100000"
    cal.save_event(
        ics(
            timed(
                "p1",
                "TOPSECRET-TITLE",
                start,
                "20261201T110000",
                "LOCATION:TOPSECRET-PLACE",
                "DESCRIPTION:TOPSECRET-NOTES",
                'ATTENDEE;CN="Topsecret Person":mailto:topsecret@example.com',
            )
        )
    )
    return cal


def test_facts_cover_what_each_code_path_depends_on(config, populated):
    facts = run(config).facts
    assert facts["calendar_access"] is True and facts["time_range_query_status"] == 207
    assert facts["scheduling_advertised"] in (True, False)
    for key in (
        "supported_reports",
        "results_carry_etags",
        "server_side_expand_status",
        "free_busy_query_status",
        "text_match_folds_ascii_case",
        "move_allowed_on_resource",
        "etag_header_on_resource",
        "sync_token_offered",
    ):
        assert key in facts, key
    assert "calendar-query" in facts["supported_reports"]


@pytest.mark.parametrize("flag", [[], ["--json"]])
def test_nothing_from_the_calendar_or_the_connection_is_printed(config, populated, capsys, monkeypatch, flag):
    # main() scrubs the credentials from the environment as the server does, so set them per call.
    for name, value in (("CALDAV_URL", f"{BASE}/{USER}/"), ("CALDAV_USERNAME", USER), ("CALDAV_PASSWORD", PASSWORD)):
        monkeypatch.setenv(name, value)
    assert main(flag) == 0
    out = capsys.readouterr().out
    for secret in (*SECRETS, PASSWORD, f"{BASE}/{USER}", populated.id, "127.0.0.1"):
        assert secret not in out, f"{secret!r} leaked into the probe output"
    if flag:
        assert json.loads(out)["facts"]["calendars"] >= 1


def test_the_report_is_plain_lines_of_facts(config, populated):
    text = render(run(config))
    assert "time_range_query_status" in text and "\n" in text


def test_the_probe_writes_nothing(config, populated):
    before = populated.get_event_by_uid("p1").etag
    run(config)
    assert populated.get_event_by_uid("p1").etag == before and len(populated.events()) == 1


def test_a_bad_login_is_reported_as_a_class_and_status_not_a_message(config, monkeypatch, capsys):
    monkeypatch.setenv("CALDAV_URL", f"{BASE}/{USER}/")
    monkeypatch.setenv("CALDAV_USERNAME", USER)
    monkeypatch.setenv("CALDAV_PASSWORD", "definitely-wrong")
    assert main([]) == 1
    out = capsys.readouterr().out
    assert "PROBLEM" in out and "definitely-wrong" not in out


def test_missing_configuration_is_a_usage_error(monkeypatch, capsys):
    for name in ("CALDAV_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert main([]) == 2
    assert "CALDAV_URL" in capsys.readouterr().err
