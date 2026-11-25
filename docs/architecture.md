# Architecture

```
caldav_mcp/
  app.py          assemble the FastMCP server, middleware, /health; main()
  config.py       environment -> immutable Config (strict/tolerant parsing, scrubs secrets)
  catalogue.py    the 11 tool names, declared; READ / WRITE / ESSENTIAL sets
  toolfilter.py   CALDAV_ALLOW_TOOLS / CALDAV_DENY_TOOLS / read-only -> enabled set
  httpauth.py     ASGI API-key middleware; ratelimit.py sliding-window limiter
  session.py      the DAVClient, one lock, cached calendar list, name/id resolution
  dates.py        keywords and ISO values -> a Range (DST-correct, inclusive date ends)
  events.py       VEVENT -> EventView -> compact dict (sanitised, flagged)
  filters.py      query/where -> tree; local meaning; server-side superset plan
  engine.py       time-range query (+ pushed-down text), local re-check, series detail
  inputs.py       validation of everything written
  series.py       recurrence scopes as pure functions over icalendar objects
  ops.py          create / update / delete / move / attendees against the server
  results.py      compact JSON, markers, byte budget; errors.py ToolError mapping
  sanitize.py     untrusted-text handling
  probe.py        read-only capability report (caldav-mcp-probe)
  tools/          read.py, write.py, attendees.py: schemas and descriptions only
```

## Data flow of a read

`tool` → `dates.resolve_range` → `session.resolve` (calendars) → `filters.build_filter` → `engine.fetch_events`
(one `calendar-query` per calendar: time-range, plus `text-match` where the plan says the server will agree;
python-caldav expands recurrences) → local `matches` re-check → `events.shape` (sanitise, flag) →
`results.untrusted_result` (marker, byte budget, notes).

## Decisions worth knowing

* **Stateless HTTP.** There is no per-session state to lose, so a restart does not strand clients.
* **One connection, one lock.** One account, one agent: each call is a short critical section bounded by the request
  timeout. The calendar list is cached for five minutes and refreshed when a name is not found.
* **Whole-resource writes.** The old tools read the first component and wrote it back, destroying exceptions to
  repeating events. Now the full resource is read, edited in place and written back, conditional on its ETag.
* **Server where it is correct, client where it is not.** Time range: server. Text: server for ASCII `contains` on
  text properties, always re-checked locally. Recurrence expansion: client (consistent across servers). Free/busy:
  computed from events, because that can name the conflict.
* **Calendars by id or name.** Names are chosen by whoever shared the calendar and need not be unique, so an
  ambiguous name is an error listing ids, never a silent first match.
* **Python's same-zone subtraction is wall-clock.** Elapsed time is always computed via UTC.

## Removed from the fork's parent

Header and "pro" modes, the SQLite config store and its CLI, credential encryption, multi-remote fan-out and dotted
calendar addressing: none were needed for a single account, and together they were about half the code.
