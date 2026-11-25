# Changelog

## 0.3.0 - unreleased

A rewrite for an LLM agent doing everyday calendar work. **Breaking**; see "Migrating" below.

### Added
- Relative and flexible dates for reads (`today`, `tomorrow`, `this_weekend`, `last_month`, `next_14_days`, weekday
  names), a configurable week start, inclusive date-only ends, and an echo of the exact range and the current time in
  every answer.
- `caldav_search_events`: several words, per-field conditions, `all`/`any`/`none`, ordering, a time range, server-side
  narrowing where the server provably agrees (re-checked locally), and an account of where matching happened.
- `scope` (`series`, `instance`, `this_and_future`) for changing and deleting repeating events; required, never guessed.
- Conditional writes (ETag / `If-Match`), idempotent create, overlap report on create, `clear` for removing fields,
  reminders, categories, all-day events and repeat rules on create and update.
- `CALDAV_ALLOW_TOOLS` / `CALDAV_DENY_TOOLS` (names, one `prefix*`, or `essential`); unknown names stop the server.
- Untrusted-text handling: invisible characters, auto-fetch markup, injection-shape flags, fenced single events.
- `GET /health`; `caldav-mcp-probe` (read-only capability report that prints no event content).
- Integration tests against real Radicale; CI starts one.

### Changed
- Reads cover **all calendars** unless some are named (before, the first calendar only, silently).
- Answers are one compact JSON object (`untrusted`, `source`, ...) instead of `{status, message, data}`; failures are
  `isError` tool errors with actionable text. Long answers are cut to 60 kB by whole events, with a note.
- `caldav_get_freebusy` returns merged busy blocks, free gaps and `available` (before, a list of events).
- `caldav_move_event` keeps the event's uid and uses WebDAV `MOVE` (before, a copy with a new uid).
- Updating no longer destroys moved or cancelled occurrences of a repeating event.
- The API key is required to start and checked before any MCP request, `tools/list` included; the rate limiter uses
  the TCP peer instead of a header nobody could set.
- Python 3.15, FastMCP 4, python-caldav 3.7, icalendar 7.3; `Containerfile` on Debian trixie; no compose file.

### Removed
- Tools: `caldav_get_today_events`, `caldav_get_week_events` (use `start="today"` / `"this_week"`),
  `caldav_list_attendees` (attendees are in `caldav_get_event_by_uid`).
- Header mode (`X-Caldav-*`), "pro" mode (`DB_CONFIG_ENABLED`, SQLite store, `caldav-mcp-config` CLI, credential
  encryption, multi-remote fan-out, dotted addressing), `CALDAV_MCP_LOG_FORMAT`, `server.py`.
- Parameters: `attendees` and `priority` on create (invitations are a separate, deliberate step); empty strings meaning
  "unchanged" on update.

### Fixed
- Unknown UID answered "Internal error" instead of not found.
- An unknown or unavailable time zone silently became UTC (it is now a startup error, and `tzdata` ships with the image).
- A mistyped `CALDAV_MCP_CALDAV_VERIFY_SSL` could disable certificate verification.
- Search downloaded every event and ignored recurrence; the date and time handling had inclusive/exclusive ambiguities.

### Migrating
New settings: `CALDAV_ALLOW_TOOLS`, `CALDAV_DENY_TOOLS`, `CALDAV_MCP_ALLOW_NO_AUTH`, `CALDAV_MCP_TRUST_PROXY`,
`CALDAV_ALLOW_PLAINTEXT`, `CALDAV_DEFAULT_CALENDAR`, `CALDAV_MAX_EVENTS`, `CALDAV_WEEK_START`,
`CALDAV_TIMEOUT_SECONDS`. Update tool allow-lists in clients: drop the three removed tools. Parameter names changed
(`calendar_name` is `calendars` on reads and `calendar` elsewhere); see `docs/tools.md`.

## 0.2.1 and earlier

See the upstream project, [gelse/caldav-mcp](https://github.com/gelse/caldav-mcp).
