"""Result shaping: markers, byte budget, whole-entry drops, honest notes."""

import json

import pytest

from caldav_mcp.errors import ResultTooLargeError
from caldav_mcp.results import budget, dumps, emitted_bytes, fenced_untrusted_result, own_words_result, untrusted_result
from tests.helpers import structured, text


def test_untrusted_result_is_marked_and_both_channels_agree():
    result = untrusted_result({"events": [{"summary": "x"}]})
    assert structured(result)["untrusted"] is True
    assert structured(result)["source"] == "caldav"
    assert json.loads(text(result, 0)) == structured(result)


def test_content_cannot_switch_the_marker_off():
    result = untrusted_result({"untrusted": False, "source": "me", "events": []})
    assert structured(result)["untrusted"] is True
    assert structured(result)["source"] == "caldav"


def test_own_words_carry_no_marker():
    assert "untrusted" not in structured(own_words_result({"deleted": 1}))


def test_json_is_compact_and_keeps_unicode():
    assert dumps({"a": "Müller", "b": [1, 2]}) == '{"a":"Müller","b":[1,2]}'


def test_budget_is_a_no_op_when_it_fits():
    data = {"events": [{"n": i} for i in range(3)]}
    assert budget(data, "x") is data


def test_budget_drops_whole_entries_and_says_so():
    events = [{"n": i, "pad": "x" * 100} for i in range(100)]
    out = budget({"events": events}, "Narrow the range.", max_bytes=2000)
    assert emitted_bytes(out) <= 2000
    assert 0 < len(out["events"]) < 100
    assert out["events"] == events[: len(out["events"])]
    assert "left out" in out["notes"][0] and "Narrow the range." in out["notes"][0]
    json.loads(dumps(out))


def test_budget_keeps_existing_notes():
    out = budget({"events": [{"p": "x" * 100}] * 50, "notes": ["earlier"]}, "f", max_bytes=1500)
    assert out["notes"][0] == "earlier" and len(out["notes"]) == 2


def test_budget_refuses_rather_than_return_an_oversized_answer():
    with pytest.raises(ResultTooLargeError):
        budget({"events": [{"p": "x" * 5000}]}, "Ask for this event alone.", max_bytes=1000)


def test_bytes_not_characters_are_measured():
    assert emitted_bytes({"s": "ü" * 10}) > len(dumps({"s": "ü" * 10}))


def test_fenced_result_has_warning_fence_and_structured_value():
    result = fenced_untrusted_result({"event": {"uid": "u"}}, "Title: hi", ["calendar-command"])
    assert text(result, 0).startswith("!! WARNING")
    assert "BEGIN UNTRUSTED EVENT CONTENT" in text(result, 0)
    assert structured(result)["untrusted"] is True


def test_fenced_result_without_signals_has_no_warning():
    result = fenced_untrusted_result({"event": {}}, "Title: hi", [])
    assert not text(result, 0).startswith("!!")
