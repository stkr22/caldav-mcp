"""Capability probe: what does this CalDAV server support, without showing what is in it.

Run it against the real server, with the same environment as the MCP server::

    caldav-mcp-probe            # or: python -m caldav_mcp.probe

It is **read-only** (OPTIONS, PROPFIND, REPORT and HEAD; never a write) and prints **only**
capability facts: HTTP status codes, protocol feature names, counts, booleans, timings. It never
prints an event's title, location, description, attendee, a calendar's name, an address or the URL, so
its output is safe to paste into an issue.

What it answers, because each one decides a code path in the server:

* is scheduling advertised (adding an attendee may then email an invitation)?
* does the server offer MOVE, free-busy-query, sync-collection, server-side expansion?
* does a time-range query work, and do the results carry ETags (conditional writes)?
* does text-match fold case (compared by counting, never by reading)?
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from caldav.davclient import DAVClient
from lxml import etree  # ty: ignore[missing-module-member]  # lxml ships no type information

from caldav_mcp.config import Config, ConfigError, load_config
from caldav_mcp.sanitize import quoted

DAV = "{DAV:}"
CALDAV = "{urn:ietf:params:xml:ns:caldav}"
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
_WINDOW_DAYS = 90


@dataclass
class Report:
    """The facts found, as plain values."""

    facts: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def set(self, key: str, value: Any) -> None:
        """Record one fact."""
        self.facts[key] = value


def _xml(body: bytes | str | None) -> Any:
    if not body:
        return None
    try:
        return etree.fromstring(body if isinstance(body, bytes) else body.encode(), _PARSER)
    except etree.XMLSyntaxError:
        return None


def _time(call: Any, *args: Any, **kwargs: Any) -> tuple[Any, int]:
    started = time.monotonic()
    result = call(*args, **kwargs)
    return result, round((time.monotonic() - started) * 1000)


def _status(response: Any) -> int:
    return int(getattr(response, "status", 0) or 0)


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", {}) or {}
    for key, value in headers.items():
        if key.lower() == name.lower():
            return str(value)
    return ""


def _tokens(value: str) -> list[str]:
    return sorted({t.strip() for t in value.replace(";", ",").split(",") if t.strip()})


_CALENDAR_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><D:getetag/></D:prop>
  <C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">
    <C:time-range start="{start}" end="{end}"/>{extra}
  </C:comp-filter></C:comp-filter></C:filter>
</C:calendar-query>"""

_EXPAND_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><C:calendar-data><C:expand start="{start}" end="{end}"/></C:calendar-data></D:prop>
  <C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">
    <C:time-range start="{start}" end="{end}"/>
  </C:comp-filter></C:comp-filter></C:filter>
</C:calendar-query>"""

_FREEBUSY = """<?xml version="1.0" encoding="utf-8"?>
<C:free-busy-query xmlns:C="urn:ietf:params:xml:ns:caldav">
  <C:time-range start="{start}" end="{end}"/>
</C:free-busy-query>"""

_REPORT_SET = (
    '<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:supported-report-set/><D:sync-token/>'
    "</D:prop></D:propfind>"
)


def _text_match(prop: str, term: str) -> str:
    return (
        f'<C:prop-filter name="{prop}"><C:text-match collation="i;ascii-casemap">{term}</C:text-match></C:prop-filter>'
    )


def _count(response: Any) -> tuple[int, bool]:
    """Return how many resources a multistatus names and whether every one carries an ETag."""
    root = _xml(getattr(response, "raw", None))
    if root is None:
        return 0, False
    responses = root.findall(f"{DAV}response")
    with_etag = [r for r in responses if r.find(f".//{DAV}getetag") is not None]
    return len(responses), bool(responses) and len(with_etag) == len(responses)


def run(config: Config, client: DAVClient | None = None) -> Report:
    """Probe the server in *config* and return the facts."""
    report = Report()
    client = client or DAVClient(
        url=config.url,
        username=config.username,
        password=config.password,
        timeout=config.timeout,
        ssl_verify_cert=config.verify_ssl,
        enable_rfc6764=False,
    )
    report.set("tls", config.url.lower().startswith("https://"))
    report.set("tls_verification", config.verify_ssl)
    try:
        principal = client.principal()
        calendars = principal.calendars()
    except Exception as exc:
        status = getattr(exc, "status", None)
        report.problems.append(
            f"could not list calendars: {type(exc).__name__}" + (f" (HTTP {status})" if status else "")
        )
        return report
    report.set("calendars", len(calendars))
    report.set("calendars_with_events", _eventful(calendars))
    _options(report, client, str(calendars[0].url) if calendars else config.url)
    if not calendars:
        report.problems.append("the account has no calendars, so queries could not be probed")
        return report
    target = next((c for c in calendars if "VEVENT" in _components(c)), calendars[0])
    _reports(report, client, target)
    _queries(report, client, target)
    return report


def _components(calendar: Any) -> list[str]:
    try:
        return [str(c).upper() for c in (calendar.get_supported_components() or [])]
    except Exception:
        return []


def _eventful(calendars: list[Any]) -> int:
    return sum(1 for c in calendars if not _components(c) or "VEVENT" in _components(c))


def _options(report: Report, client: DAVClient, url: str) -> None:
    response, ms = _time(client.request, url, "OPTIONS")
    report.set("options_status", _status(response))
    report.set("options_ms", ms)
    dav = _tokens(_header(response, "DAV"))
    allow = _tokens(_header(response, "Allow"))
    report.set("dav_classes", dav)
    report.set("allowed_methods", allow)
    report.set("calendar_access", "calendar-access" in dav)
    # Scheduling decides whether adding an attendee can send an invitation.
    report.set("scheduling_advertised", any(token.startswith("calendar-auto-schedule") for token in dav))
    report.set("move_allowed_on_collection", "MOVE" in allow)


def _reports(report: Report, client: DAVClient, calendar: Any) -> None:
    response, _ = _time(
        client.request, str(calendar.url), "PROPFIND", _REPORT_SET, {"Depth": "0", "Content-Type": "application/xml"}
    )
    report.set("report_set_status", _status(response))
    root = _xml(getattr(response, "raw", None))
    supported: list[str] = []
    sync_token = False
    if root is not None:
        supported = sorted(
            {
                etree.QName(e).localname
                for e in root.iter()
                if etree.QName(e).namespace in (DAV[1:-1], CALDAV[1:-1])
                and e.getparent() is not None
                and etree.QName(e.getparent()).localname == "report"
            }
        )
        token = root.find(f".//{DAV}sync-token")
        sync_token = token is not None and bool((token.text or "").strip())
    report.set("supported_reports", supported)
    report.set("sync_token_offered", sync_token)
    report.set("report_free_busy_query_listed", "free-busy-query" in supported)
    report.set("report_sync_collection_listed", "sync-collection" in supported)


def _queries(report: Report, client: DAVClient, calendar: Any) -> None:
    now = datetime.now(UTC)
    window = {
        "start": now.strftime("%Y%m%dT%H%M%SZ"),
        "end": (now + timedelta(days=_WINDOW_DAYS)).strftime("%Y%m%dT%H%M%SZ"),
    }
    url = str(calendar.url)
    headers = {"Depth": "1", "Content-Type": "application/xml"}

    def query(extra: str = "") -> Any:
        return client.request(url, "REPORT", _CALENDAR_QUERY.format(extra=extra, **window), headers)

    response, ms = _time(query)
    total, all_etags = _count(response)
    report.set("time_range_query_status", _status(response))
    report.set("time_range_query_ms", ms)
    report.set("events_in_next_90_days", total)
    report.set("results_carry_etags", all_etags if total else "unknown (no events in the window)")

    expand, _ = _time(client.request, url, "REPORT", _EXPAND_QUERY.format(**window), headers)
    report.set("server_side_expand_status", _status(expand))

    free_busy, _ = _time(
        client.request, url, "REPORT", _FREEBUSY.format(**window), {"Depth": "1", "Content-Type": "application/xml"}
    )
    body = getattr(free_busy, "raw", b"") or b""
    report.set("free_busy_query_status", _status(free_busy))
    report.set(
        "free_busy_query_returns_vfreebusy", b"VFREEBUSY" in (body if isinstance(body, bytes) else body.encode())
    )

    _case_folding(report, query)
    _etag_header(report, client, url, response)


def _case_folding(report: Report, query: Any) -> None:
    """Compare match counts for the same letter in two cases; nothing is read, only counted."""
    lower, upper = (_count(query(_text_match("SUMMARY", term)))[0] for term in ("e", "E"))
    report.set("text_match_status", _status(query(_text_match("SUMMARY", "e"))))
    if _count(query())[0] == 0:
        report.set("text_match_folds_ascii_case", "unknown (no events in the window)")
    else:
        report.set("text_match_counts_e_vs_E", [lower, upper])
        report.set("text_match_folds_ascii_case", lower == upper)


def _etag_header(report: Report, client: DAVClient, url: str, response: Any) -> None:
    root = _xml(getattr(response, "raw", None))
    href = root.find(f".//{DAV}href") if root is not None else None
    if href is None or not (href.text or "").strip():
        report.set("etag_header_on_resource", "unknown (no events in the window)")
        return
    base = url.split("/", 3)
    resource = (
        f"{base[0]}//{base[2]}{(href.text or '').strip()}" if (href.text or "").startswith("/") else (href.text or "")
    )
    head = client.request(resource, "HEAD")
    etag = _header(head, "ETag")
    report.set("etag_header_status", _status(head))
    report.set("etag_header_on_resource", bool(etag))
    report.set("etag_is_weak", etag.startswith("W/") if etag else None)
    options = client.request(resource, "OPTIONS")
    report.set("move_allowed_on_resource", "MOVE" in _tokens(_header(options, "Allow")))


def render(report: Report) -> str:
    """Format the facts as aligned lines."""
    width = max((len(k) for k in report.facts), default=0)
    lines = [
        f"{key.ljust(width)}  {json.dumps(value) if not isinstance(value, str) else value}"
        for key, value in report.facts.items()
    ]
    lines += [f"PROBLEM  {quoted(p, 200)}" for p in report.problems]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``caldav-mcp-probe``."""
    parser = argparse.ArgumentParser(prog="caldav-mcp-probe", description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="print the facts as one JSON object")
    args = parser.parse_args(argv)
    try:
        config = load_config(os.environ, serving=False)
    except ConfigError as exc:
        sys.stderr.write(f"caldav-mcp-probe: {exc}\n")
        return 2
    report = run(config)
    sys.stdout.write(
        (json.dumps({"facts": report.facts, "problems": report.problems}, indent=2) if args.json else render(report))
        + "\n"
    )
    return 1 if report.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
