"""CALDAV_ALLOW_TOOLS / CALDAV_DENY_TOOLS: three forms, deny subtracted, typos fatal."""

import pytest

from caldav_mcp.catalogue import ALL_TOOLS, ESSENTIAL_TOOLS, READ_TOOLS, WRITE_TOOLS
from caldav_mcp.config import ConfigError
from caldav_mcp.toolfilter import build_tool_filter


def enabled(allow=None, deny=None, read_only=False):
    return build_tool_filter(allow, deny, read_only=read_only).enabled


def test_default_is_everything():
    assert enabled() == frozenset(ALL_TOOLS)


def test_comma_separated_names_with_or_without_prefix():
    assert enabled("caldav_get_events, search_events") == {"caldav_get_events", "caldav_search_events"}


def test_trailing_star_prefix():
    assert enabled("caldav_get_*") == {"caldav_get_event_by_uid", "caldav_get_events", "caldav_get_freebusy"}
    assert enabled("get_*") == {"caldav_get_event_by_uid", "caldav_get_events", "caldav_get_freebusy"}


def test_bare_star_is_every_tool():
    assert enabled("*") == frozenset(ALL_TOOLS)


def test_essential_keyword_is_case_insensitive():
    assert enabled("Essential") == frozenset(ESSENTIAL_TOOLS)


def test_essential_leaves_out_everything_irreversible():
    chosen = enabled("essential")
    assert not chosen & {"caldav_delete_event", "caldav_move_event", "caldav_add_attendee", "caldav_remove_attendee"}


def test_deny_is_subtracted_from_allow_and_from_default():
    assert "caldav_delete_event" not in enabled(deny="caldav_delete_event")
    assert enabled("caldav_get_*", "caldav_get_freebusy") == {"caldav_get_event_by_uid", "caldav_get_events"}


def test_deny_accepts_the_same_three_forms():
    assert not enabled(deny="caldav_add_*") & {"caldav_add_attendee"}
    assert enabled("essential", "caldav_create_event") == frozenset(ESSENTIAL_TOOLS) - {"caldav_create_event"}


@pytest.mark.parametrize("value", ["caldav_get_event", "nonsense", "caldav_get_events,typo"])
def test_unknown_names_fail_at_startup(value):
    with pytest.raises(ConfigError, match="unknown tool"):
        enabled(value)


def test_a_typo_in_deny_is_fatal_too():
    with pytest.raises(ConfigError, match="CALDAV_DENY_TOOLS"):
        enabled(deny="caldav_delet_event")


@pytest.mark.parametrize("value", ["caldav_*_events", "caldav_get_*,caldav_x", "**", "get_*,essential"])
def test_malformed_patterns_are_refused(value):
    with pytest.raises(ConfigError):
        enabled(value)


def test_pattern_matching_nothing_is_refused():
    with pytest.raises(ConfigError, match="matches no tool"):
        enabled("zzz*")


def test_empty_entry_is_refused():
    with pytest.raises(ConfigError):
        enabled("caldav_get_events,,")


def test_everything_denied_is_refused():
    with pytest.raises(ConfigError, match="no tools are left"):
        enabled(deny="*")


class TestReadOnly:
    def test_read_only_hides_every_write_tool(self):
        assert enabled(read_only=True) == frozenset(READ_TOOLS)

    def test_read_only_wins_over_an_allow_list_and_reports_what_it_suppressed(self):
        result = build_tool_filter("caldav_get_events,caldav_delete_event", None, read_only=True)
        assert result.enabled == {"caldav_get_events"}
        assert result.suppressed_by_read_only == ("caldav_delete_event",)

    def test_naming_only_write_tools_in_read_only_mode_is_refused(self):
        with pytest.raises(ConfigError, match="no tools are left"):
            enabled("caldav_delete_event", read_only=True)

    def test_write_tools_are_known_names_even_in_read_only_mode(self):
        # "unknown tool" would be the wrong answer: the tool exists, read-only suppresses it.
        assert build_tool_filter("caldav_get_events,caldav_move_event", None, read_only=True).enabled


def test_catalogue_is_consistent():
    assert set(ESSENTIAL_TOOLS) <= set(ALL_TOOLS)
    assert not set(READ_TOOLS) & set(WRITE_TOOLS)
    assert len(ALL_TOOLS) == len(set(ALL_TOOLS)) == 11
