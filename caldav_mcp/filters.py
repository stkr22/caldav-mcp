r"""Search filters: what the caller asks, what the server is sent, what is checked again here.

Three representations, and the gap between them is the point of this module
(the design is the sibling carddav-mcp's ``filter.ts``):

* **What a caller writes** is flat - ``query`` plus ``all`` / ``any`` / ``none``
  lists of ``{field, op, value}`` leaves. A recursive schema is the one thing MCP
  clients disagree about, and models write flat shapes more reliably.
* **What is evaluated** is a tree, because ``query`` needs one: ``"anna dentist"``
  is an AND of two words, each an OR over several fields.
* **What is sent** is only a list of ``prop-filter`` text-matches inside the
  time-range query - all that RFC 4791 can carry, since the properties of a
  ``comp-filter`` are ANDed and there is no negation worth trusting.

The server filter is always a **superset** of the tree's matches - a card the
server leaves out can never be recovered - and :func:`matches` makes it exact
again on what comes back. The server filter is an optimisation; the local check
is the meaning.

Pushed down only when the server would provably agree: ``contains`` semantics on a
text property, an ASCII-only term (RFC 4791's ``i;ascii-casemap`` folds ASCII case
and nothing else, so "Zahnärztin" would be missed by a server that "matched"),
and no ``\\``, ``,`` or ``;`` (stored escaped; servers differ on which side they
compare).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from caldav_mcp.errors import ToolInputError
from caldav_mcp.events import EventView
from caldav_mcp.sanitize import has_control_characters, quoted, strip_invisible

FILTER_FIELDS = ("summary", "location", "description", "categories", "attendee", "organizer", "status")
FILTER_OPS = ("contains", "equals", "starts_with", "ends_with", "exists", "missing")
#: What a bare ``query`` word is matched against when ``fields`` is not given.
DEFAULT_QUERY_FIELDS = ("summary", "location", "description", "categories", "attendee")
#: Properties the server can text-match (the others hold addresses or enums).
PUSHABLE_FIELDS = frozenset({"summary", "location", "description", "categories"})
MAX_LEAVES = 20
MAX_QUERY_TERMS = 8
MAX_VALUE_LENGTH = 512

_UNSAFE_FOR_WIRE = re.compile(r"[\\,;]")


@dataclass(frozen=True)
class Leaf:
    """One condition on one field."""

    field: str
    op: str
    value: str = ""


@dataclass(frozen=True)
class Node:
    """A boolean node: ``and``, ``or``, ``not`` or a ``leaf``."""

    kind: Literal["and", "or", "not", "leaf"]
    children: tuple[Node, ...] = ()
    leaf: Leaf | None = None


@dataclass
class ServerPlan:
    """What can be sent to the server, and what is left for the local check."""

    #: ``(property, term)`` text-matches sent as prop-filters.
    terms: list[tuple[str, str]] = field(default_factory=list)
    #: True when the server's answer is exactly the tree's, before the local re-check.
    exact: bool = False
    #: Why something was not pushed down, for the answer's notes.
    reasons: list[str] = field(default_factory=list)


def _leaf(item: dict[str, Any]) -> Node:
    name, op = item.get("field"), item.get("op")
    value = item.get("value")
    if name not in FILTER_FIELDS:
        raise ToolInputError(f"where: unknown field {quoted(str(name), 30)!r}. Use one of: {', '.join(FILTER_FIELDS)}.")
    if op not in FILTER_OPS:
        raise ToolInputError(f"where: unknown op {quoted(str(op), 30)!r}. Use one of: {', '.join(FILTER_OPS)}.")
    needs_value = op not in ("exists", "missing")
    if needs_value and not (isinstance(value, str) and strip_invisible(value).strip()):
        raise ToolInputError(f"where: op {op!r} needs a non-empty value.")
    if not needs_value and value not in (None, ""):
        raise ToolInputError(f"where: op {op!r} takes no value; leave it out.")
    if isinstance(value, str) and (len(value) > MAX_VALUE_LENGTH or has_control_characters(value)):
        raise ToolInputError(f"where: values must be one line of at most {MAX_VALUE_LENGTH} characters.")
    return Node("leaf", leaf=Leaf(str(name), str(op), (value or "").strip()))


def build_filter(
    query: str = "",
    fields: list[str] | None = None,
    where: dict[str, Any] | None = None,
) -> Node | None:
    """Build the evaluation tree, or ``None`` when no condition was given."""
    parts: list[Node] = []
    if query.strip():
        words = query.split()
        if len(words) > MAX_QUERY_TERMS:
            raise ToolInputError(f"query has {len(words)} words; at most {MAX_QUERY_TERMS} are matched. Use fewer.")
        chosen = tuple(fields) if fields else DEFAULT_QUERY_FIELDS
        bad = [f for f in chosen if f not in FILTER_FIELDS]
        if bad:
            raise ToolInputError(
                f"fields: unknown {', '.join(quoted(b, 30) for b in bad)}. Use: {', '.join(FILTER_FIELDS)}."
            )
        for word in words:
            if has_control_characters(word) or len(word) > MAX_VALUE_LENGTH:
                raise ToolInputError("query must be plain one-line text.")
            leaves = tuple(Node("leaf", leaf=Leaf(f, "contains", word)) for f in chosen)
            parts.append(leaves[0] if len(leaves) == 1 else Node("or", leaves))
    elif fields:
        raise ToolInputError("fields only narrows `query`; give a query too, or use where.")
    where = where or {}
    unknown = set(where) - {"all", "any", "none"}
    if unknown:
        raise ToolInputError(f"where: unknown key(s) {', '.join(sorted(map(str, unknown)))}. Use all, any, none.")
    all_, any_, none_ = (where.get(k) or [] for k in ("all", "any", "none"))
    if len(all_) + len(any_) + len(none_) > MAX_LEAVES:
        raise ToolInputError(f"where: at most {MAX_LEAVES} conditions in total.")
    parts.extend(_leaf(i) for i in all_)
    if any_:
        parts.append(Node("or", tuple(_leaf(i) for i in any_)))
    if none_:
        parts.append(Node("not", (Node("or", tuple(_leaf(i) for i in none_)),)))
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else Node("and", tuple(parts))


# --- local evaluation: the meaning of a filter ------------------------------------------------------------------------


def fold(value: str) -> str:
    """Normalise for comparison: NFKC, invisible characters removed, case-folded."""
    return strip_invisible(unicodedata.normalize("NFKC", value)).strip().casefold()


def _values(view: EventView, name: str) -> list[str]:
    """Return the searchable values of one field (empty values are absent, so ``missing`` works)."""
    single = {
        "summary": view.summary,
        "location": view.location,
        "description": view.description,
        "status": view.status,
    }
    if name in single:
        return [single[name]] if single[name] else []
    if name == "categories":
        return list(view.categories)
    if name == "attendee":
        return [a.search_text() for a in view.attendees]
    return [view.organizer.search_text()] if name == "organizer" and view.organizer else []


def _leaf_matches(view: EventView, leaf: Leaf) -> bool:
    values = _values(view, leaf.field)
    if leaf.op == "exists":
        return bool(values)
    if leaf.op == "missing":
        return not values
    needle = fold(leaf.value)
    if not needle:
        return False
    tests = {
        "contains": lambda v: needle in v,
        "equals": lambda v: v == needle,
        "starts_with": lambda v: v.startswith(needle),
        "ends_with": lambda v: v.endswith(needle),
    }
    check = tests[leaf.op]
    return any(check(fold(v)) for v in values)


def matches(view: EventView, node: Node) -> bool:
    """Whether *view* satisfies the tree."""
    if node.kind == "leaf" and node.leaf is not None:
        return _leaf_matches(view, node.leaf)
    if node.kind == "and":
        return all(matches(view, c) for c in node.children)
    if node.kind == "or":
        return any(matches(view, c) for c in node.children)
    return not matches(view, node.children[0])


# --- the server filter: a superset, never a subset --------------------------------------------------------------------


def _pushable(leaf: Leaf, reasons: list[str]) -> bool:
    if leaf.op in ("exists", "missing") or leaf.field not in PUSHABLE_FIELDS:
        return False
    if not leaf.value.isascii():
        reasons.append("a non-ASCII term is matched here, because servers only fold ASCII case")
        return False
    if _UNSAFE_FOR_WIRE.search(leaf.value):
        reasons.append(
            "a term with a backslash, comma or semicolon is matched here, because servers disagree on escaping"
        )
        return False
    return True


def plan_server_filter(node: Node | None) -> ServerPlan:
    """Choose what to send to the server for *node* (sending nothing is always correct).

    Only the leaves of the top-level AND are necessary conditions, so only those can be
    pushed; an OR across fields, a negation or an ``any`` group never can.
    """
    plan = ServerPlan()
    if node is None:
        plan.exact = True
        return plan
    top = list(node.children) if node.kind == "and" else [node]
    for child in top:
        if child.kind == "leaf" and child.leaf is not None and _pushable(child.leaf, plan.reasons):
            plan.terms.append((child.leaf.field, child.leaf.value))
    # Exact only if every conjunct went to the server as a plain contains.
    plan.exact = len(plan.terms) == len(top) and all(c.leaf is not None and c.leaf.op == "contains" for c in top)
    plan.reasons = sorted(set(plan.reasons))
    return plan
