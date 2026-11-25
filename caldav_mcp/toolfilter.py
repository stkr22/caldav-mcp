"""Server-side allow/deny list for tools.

``CALDAV_ALLOW_TOOLS`` takes exactly one of three forms, like the sibling
carddav-mcp:

* comma-separated tool names: ``caldav_get_events,caldav_create_event``
* one prefix ending in a single ``*``: ``caldav_get_*`` (a bare ``*`` is every tool)
* the keyword ``essential``

``CALDAV_DENY_TOOLS`` has the same shape and is subtracted. Names may be written
with or without the ``caldav_`` prefix. Unknown names and patterns that match
nothing refuse to start: a typo in a *deny* list would otherwise expose the very
tool it was meant to hide. Read-only mode is applied last and always wins.
"""

from __future__ import annotations

from dataclasses import dataclass

from caldav_mcp.catalogue import ALL_TOOLS, ESSENTIAL_TOOLS, PREFIX, READ_TOOLS, WRITE_TOOLS
from caldav_mcp.config import ConfigError
from caldav_mcp.sanitize import quoted


@dataclass(frozen=True)
class ToolFilter:
    """Which tools get registered, and which were named but suppressed."""

    enabled: frozenset[str]
    #: Named by the allow list but removed by read-only mode (reported at startup).
    suppressed_by_read_only: tuple[str, ...]


def _canonical(name: str) -> str:
    name = name.strip().lower()
    return name if name.startswith(PREFIX) else PREFIX + name


def parse_tool_list(raw: str, variable: str) -> frozenset[str]:
    """Resolve one list variable to a set of known tool names."""
    value = raw.strip()
    if value.lower() == "essential":
        return frozenset(ESSENTIAL_TOOLS)
    if "*" in value:
        if "," in value or not value.endswith("*") or value.count("*") != 1:
            raise ConfigError(
                f"{variable}: a pattern must be a single prefix ending in one '*' (for example caldav_get_*), "
                f"not combined with other entries - got {quoted(value, 60)!r}."
            )
        prefix = value[:-1].strip().lower()
        matched = frozenset(name for name in ALL_TOOLS if name.startswith((prefix, PREFIX + prefix)))
        if not matched:
            raise ConfigError(f"{variable}: pattern {quoted(value, 60)!r} matches no tool.")
        return matched
    entries = [_canonical(part) for part in value.split(",")]
    if any(entry == PREFIX for entry in entries):
        raise ConfigError(f"{variable} contains an empty entry.")
    unknown = [entry for entry in entries if entry not in ALL_TOOLS]
    if unknown:
        raise ConfigError(
            f"{variable}: unknown tool(s) {', '.join(quoted(u, 60) for u in unknown)}. "
            f"Known tools: {', '.join(ALL_TOOLS)}."
        )
    return frozenset(entries)


def build_tool_filter(allow: str | None, deny: str | None, *, read_only: bool) -> ToolFilter:
    """Combine allow, deny and read-only into the final tool set."""
    allowed = parse_tool_list(allow, "CALDAV_ALLOW_TOOLS") if allow is not None else frozenset(ALL_TOOLS)
    denied = parse_tool_list(deny, "CALDAV_DENY_TOOLS") if deny is not None else frozenset()
    chosen = allowed - denied
    suppressed: tuple[str, ...] = ()
    if read_only:
        suppressed = tuple(name for name in WRITE_TOOLS if name in chosen and allow is not None)
        chosen = chosen & frozenset(READ_TOOLS)
    if not chosen:
        raise ConfigError("no tools are left after CALDAV_ALLOW_TOOLS, CALDAV_DENY_TOOLS and read-only mode.")
    return ToolFilter(enabled=chosen, suppressed_by_read_only=suppressed)
