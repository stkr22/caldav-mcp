"""Search filters: the tree, the local meaning, and the superset sent to the server."""

from zoneinfo import ZoneInfo

import pytest

from caldav_mcp.errors import ToolInputError
from caldav_mcp.events import view_from_component
from caldav_mcp.filters import (
    DEFAULT_QUERY_FIELDS,
    MAX_LEAVES,
    build_filter,
    fold,
    matches,
    plan_server_filter,
)
from tests.unit.ics import timed

BERLIN = ZoneInfo("Europe/Berlin")


def ev(summary="Dentist", *extra):
    comp = timed(summary, "20261126T100000", "20261126T110000", *extra)
    return view_from_component(comp, calendar="Personal", tz=BERLIN)


def hit(view, **kw):
    node = build_filter(**kw)
    assert node is not None
    return matches(view, node)


class TestBuild:
    def test_nothing_asked_is_no_filter(self):
        assert build_filter() is None
        assert build_filter(query="  ") is None

    def test_where_must_name_known_fields_and_ops(self):
        with pytest.raises(ToolInputError, match="unknown field"):
            build_filter(where={"all": [{"field": "title", "op": "contains", "value": "x"}]})
        with pytest.raises(ToolInputError, match="unknown op"):
            build_filter(where={"all": [{"field": "summary", "op": "like", "value": "x"}]})

    @pytest.mark.parametrize(
        "leaf",
        [
            {"field": "summary", "op": "contains"},
            {"field": "summary", "op": "contains", "value": "  "},
            {"field": "summary", "op": "exists", "value": "x"},
            {"field": "summary", "op": "contains", "value": "a\nb"},
            {"field": "summary", "op": "contains", "value": "x" * 600},
        ],
    )
    def test_bad_leaves(self, leaf):
        with pytest.raises(ToolInputError):
            build_filter(where={"all": [leaf]})

    def test_unknown_where_key(self):
        with pytest.raises(ToolInputError, match="unknown key"):
            build_filter(where={"either": []})

    def test_too_many_words_and_leaves(self):
        with pytest.raises(ToolInputError, match="at most 8"):
            build_filter(query="a b c d e f g h i")
        leaves = [{"field": "summary", "op": "contains", "value": "x"}] * (MAX_LEAVES + 1)
        with pytest.raises(ToolInputError, match="at most 20"):
            build_filter(where={"all": leaves})

    def test_fields_without_query_is_an_error_not_a_silent_no_op(self):
        with pytest.raises(ToolInputError, match="fields only narrows"):
            build_filter(fields=["summary"])

    def test_unknown_query_field(self):
        with pytest.raises(ToolInputError, match="unknown"):
            build_filter(query="x", fields=["title"])

    def test_control_characters_in_query(self):
        with pytest.raises(ToolInputError):
            build_filter(query="a\x00b")


class TestLocalMeaning:
    def test_every_word_must_match_but_in_any_field(self):
        v = ev("Dentist", "LOCATION:Praxis Dr. Roth", 'ATTENDEE;CN="Anna Meier":mailto:anna@example.com')
        assert hit(v, query="dentist anna")
        assert hit(v, query="roth dentist")
        assert not hit(v, query="dentist bob")

    def test_case_and_unicode_are_folded(self):
        assert hit(ev("Zahnärztin Müller"), query="ZAHNÄRZTIN")
        assert hit(ev("Straße"), query="STRASSE")

    def test_invisible_characters_do_not_hide_an_event_from_search(self):
        assert hit(ev("Den\u200btist"), query="dentist")
        assert fold("A\u202eB") == "ab"

    def test_attendee_matches_name_or_address(self):
        v = ev("Sync", 'ATTENDEE;CN="Anna Meier":mailto:Anna.M@Example.com')
        assert hit(v, query="meier", fields=["attendee"])
        assert hit(v, query="example.com", fields=["attendee"])
        assert not hit(ev("Sync"), query="anna", fields=["attendee"])

    def test_fields_restricts_the_query(self):
        v = ev("Lunch", "LOCATION:Dentist street")
        assert hit(v, query="dentist", fields=["location"])
        assert not hit(v, query="dentist", fields=["summary"])

    @pytest.mark.parametrize(
        ("op", "value", "expected"),
        [
            ("contains", "ent", True),
            ("equals", "dentist", True),
            ("equals", "dent", False),
            ("starts_with", "den", True),
            ("starts_with", "ist", False),
            ("ends_with", "ist", True),
            ("ends_with", "den", False),
        ],
    )
    def test_operators(self, op, value, expected):
        assert hit(ev("Dentist"), where={"all": [{"field": "summary", "op": op, "value": value}]}) is expected

    def test_exists_and_missing(self):
        v = ev("Dentist", "LOCATION:Praxis")
        assert hit(v, where={"all": [{"field": "location", "op": "exists"}]})
        assert hit(v, where={"all": [{"field": "description", "op": "missing"}]})
        assert not hit(v, where={"all": [{"field": "location", "op": "missing"}]})

    def test_all_any_none_combine(self):
        v = ev("Dentist", "LOCATION:Praxis")
        any_ = {
            "any": [
                {"field": "summary", "op": "contains", "value": "nope"},
                {"field": "location", "op": "contains", "value": "praxis"},
            ]
        }
        assert hit(v, where=any_)
        assert not hit(v, where={"none": [{"field": "location", "op": "contains", "value": "praxis"}]})
        assert hit(v, where={"none": [{"field": "location", "op": "contains", "value": "elsewhere"}]})
        assert hit(
            v,
            query="dentist",
            where={
                "all": [{"field": "location", "op": "exists"}],
                "none": [{"field": "status", "op": "equals", "value": "cancelled"}],
            },
        )

    def test_none_with_several_values_means_no_value_matches(self):
        v = ev("Sync", 'ATTENDEE;CN="A":mailto:a@example.com', 'ATTENDEE;CN="B":mailto:b@example.com')
        assert not hit(v, where={"none": [{"field": "attendee", "op": "contains", "value": "a@example"}]})

    def test_query_combined_with_where_is_an_and(self):
        v = ev("Dentist", "LOCATION:Praxis")
        assert not hit(v, query="lunch", where={"all": [{"field": "location", "op": "exists"}]})

    def test_default_fields_are_the_documented_ones(self):
        assert DEFAULT_QUERY_FIELDS == ("summary", "location", "description", "categories", "attendee")


class TestServerPlan:
    def plan(self, **kw):
        return plan_server_filter(build_filter(**kw))

    def test_no_filter_sends_nothing_and_is_exact(self):
        plan = plan_server_filter(None)
        assert plan.terms == [] and plan.exact

    def test_single_field_words_are_all_pushed_and_exact(self):
        plan = self.plan(query="dentist roth", fields=["summary"])
        assert plan.terms == [("summary", "dentist"), ("summary", "roth")]
        assert plan.exact

    def test_a_query_across_several_fields_cannot_be_pushed(self):
        plan = self.plan(query="dentist")
        assert plan.terms == [] and not plan.exact

    def test_where_all_contains_is_pushed_exactly(self):
        plan = self.plan(
            where={
                "all": [
                    {"field": "summary", "op": "contains", "value": "dentist"},
                    {"field": "location", "op": "contains", "value": "praxis"},
                ]
            }
        )
        assert plan.terms == [("summary", "dentist"), ("location", "praxis")] and plan.exact

    def test_equals_is_pushed_as_a_superset_but_never_exact(self):
        plan = self.plan(where={"all": [{"field": "summary", "op": "equals", "value": "dentist"}]})
        assert plan.terms == [("summary", "dentist")] and not plan.exact

    def test_any_none_exists_and_unpushable_fields_stay_local(self):
        assert self.plan(where={"any": [{"field": "summary", "op": "contains", "value": "a"}]}).terms == []
        assert self.plan(where={"none": [{"field": "summary", "op": "contains", "value": "a"}]}).terms == []
        assert self.plan(where={"all": [{"field": "summary", "op": "exists"}]}).terms == []
        assert self.plan(where={"all": [{"field": "attendee", "op": "contains", "value": "anna"}]}).terms == []

    def test_non_ascii_terms_are_never_pushed_and_the_reason_is_reported(self):
        plan = self.plan(query="Zahnärztin", fields=["summary"])
        assert plan.terms == [] and not plan.exact
        assert any("non-ASCII" in r for r in plan.reasons)

    def test_one_ascii_one_non_ascii_term_pushes_only_the_ascii_one(self):
        plan = self.plan(query="dentist Zahnärztin", fields=["summary"])
        assert plan.terms == [("summary", "dentist")] and not plan.exact

    @pytest.mark.parametrize("term", ["a,b", "a;b", "a\\b"])
    def test_terms_the_wire_escapes_differently_stay_local(self, term):
        plan = self.plan(where={"all": [{"field": "summary", "op": "contains", "value": term}]})
        assert plan.terms == [] and any("escaping" in r for r in plan.reasons)

    def test_the_server_plan_is_a_superset_of_the_local_meaning(self):
        # Whatever is pushed must be implied by the full tree: every local match contains the pushed terms.
        node = build_filter(
            query="dentist",
            fields=["summary", "location"],
            where={"all": [{"field": "summary", "op": "starts_with", "value": "Den"}]},
        )
        plan = plan_server_filter(node)
        for field, term in plan.terms:
            assert field == "summary" and term == "Den"
        v = ev("Dentist")
        assert matches(v, node) and all(t.lower() in v.summary.lower() for _, t in plan.terms)
