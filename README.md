# caldav-mcp

An [MCP](https://modelcontextprotocol.io) server that gives an LLM agent compact, safe access to a CalDAV
calendar (Nextcloud, Radicale, Baikal, Fastmail, hosted services, ...) over Streamable HTTP.

It started as a fork of [gelse/caldav-mcp](https://github.com/gelse/caldav-mcp) and is now a different program:
built for an agent doing everyday calendar work with a small context window, where the failure that matters is a
*wrong or empty answer*, not a missing feature.

## What it does differently

- **Reads cover every calendar** unless you name some, so "what's on tomorrow" cannot silently look at only the first one.
- **Relative dates are resolved here**, in your time zone: `today`, `tomorrow`, `this_weekend`, `last_month`,
  `next_14_days`, weekday names. A date-only `end` is inclusive. Every answer states the exact range it covered
  and the current time, so a misreading is visible.
- **Recurring events are handled correctly**: expansion honours moved, cancelled and ended occurrences, and
  changes and deletions say *which* occurrences they apply to (`series`, `instance`, `this_and_future`). There is
  no default: asking to change a repeating event without a scope is an error that lists the choices.
- **Search is real search**: several words, per-field conditions, `all`/`any`/`none`, ordering, and a time range,
  pushed to the server where the server will provably agree and finished locally where it will not. The answer says
  which happened.
- **Nothing is silent**: truncated results say so, an empty result says what was searched and why nothing matched,
  a calendar that failed is reported instead of omitted, and failures are real tool errors (`isError`).
- **Writes are careful**: only the fields you give change, everything else in the stored event survives, writes are
  conditional on the version just read (ETag / `If-Match`), a retried create is a no-op, and a create reports the
  events it overlaps.
- **Event text is treated as hostile**: titles, locations, descriptions and names come from other people. They are
  stripped of invisible characters, defused against auto-fetching markup, flagged when they look like instructions,
  and returned in marked, fenced form (see [docs/security.md](docs/security.md)).

## Tools

| Tool | What it does |
|---|---|
| `caldav_list_calendars` | Calendars (id, name), the current local time and date, the zone and the first day of the week. Start here when unsure. |
| `caldav_get_events` | The agenda for a period across all (or some) calendars. `start`/`end` take dates, date-times or keywords. |
| `caldav_search_events` | Find events by words, fields, or `where` conditions; defaults to now → one year ahead. |
| `caldav_get_event_by_uid` | One event in full: description, attendees, reminders, ETag, and a series' rule and exceptions. |
| `caldav_get_freebusy` | Is a window free? Merged busy blocks with what causes them, free gaps, `available`. |
| `caldav_create_event` | New event (timed, all-day or repeating) with reminders; reports overlaps; safe to retry. |
| `caldav_update_event` | Change only the given fields; `clear` removes fields; `scope` for repeating events. |
| `caldav_delete_event` | Delete an event, one occurrence, or an occurrence and all later ones. |
| `caldav_move_event` | Move an event (with its whole series) to another calendar, keeping its uid. |
| `caldav_add_attendee`, `caldav_remove_attendee` | Attendee changes. Adding may make the server email an invitation. |

Full parameters, answers and examples: [docs/tools.md](docs/tools.md).

## Run it

```bash
cp .env.example .env     # fill in CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD, CALDAV_MCP_API_KEY
uv sync
set -a; . ./.env; set +a
uv run caldav-mcp        # or: python server.py
```

Or with Docker (`linux/amd64` and `linux/arm64`):

```bash
docker compose up --build
```

Point an MCP client at `http://host:8080/mcp` with `Authorization: Bearer <CALDAV_MCP_API_KEY>`
(or `X-Api-Key`). `GET /health` answers `ok` without a key, for probes; a plain TCP check on the port also works.

### Required settings

| Variable | |
|---|---|
| `CALDAV_URL` | The CalDAV calendar home or principal. `https://` unless the host is loopback. |
| `CALDAV_USERNAME`, `CALDAV_PASSWORD` | The account (most hosts want an app-specific password). Removed from the process environment after startup. |
| `CALDAV_MCP_API_KEY` | At least 16 characters. **The server refuses to start without it**, unless `CALDAV_MCP_ALLOW_NO_AUTH=true`. |

Everything else is optional: tool allow/deny lists, read-only mode, time zone, week start, default calendar, limits,
TLS. See [docs/configuration.md](docs/configuration.md).

### Choose what the agent can see

```bash
CALDAV_ALLOW_TOOLS=caldav_list_calendars,caldav_get_events,caldav_search_events,caldav_get_event_by_uid,caldav_get_freebusy,caldav_create_event,caldav_update_event,caldav_delete_event
CALDAV_DENY_TOOLS=caldav_move_event          # subtracted from the above
CALDAV_MCP_READ_ONLY=true                    # hides every write tool; always wins
```

`CALDAV_ALLOW_TOOLS` takes comma-separated names, **or** one prefix ending in `*` (`caldav_get_*`), **or** the
keyword `essential` (find, read, check availability, create, update; no delete, move or attendees). Unknown names
stop the server from starting.

## Check a real server before trusting it

Servers differ (text matching, free/busy, MOVE, ETags, scheduling). `caldav-mcp-probe` asks the server what it
supports, read-only, and prints **only capability facts** (status codes, supported reports, counts, booleans): never
an event, a calendar name, or the URL.

```bash
CALDAV_URL=... CALDAV_USERNAME=... CALDAV_PASSWORD=... uv run caldav-mcp-probe
```

## Development

```bash
uv sync --dev
uv run ruff check . && uv run ruff format --check . && uv run ty check
uv run pytest tests/unit                     # no network
```

The integration suite needs a CalDAV server. In the dev container, the `radicale` service in
`.devcontainer/compose.yml` provides one on `127.0.0.1:5232`; without Docker run
`tests/integration/radicale-local.sh` (set `RADICALE_PORT` to change the port, and `CALDAV_TEST_URL` to match):

```bash
uv run pytest tests/integration
```

More in [docs/contributing.md](docs/contributing.md) and [docs/architecture.md](docs/architecture.md).

## Status

Developed and tested against Radicale. Other servers differ in small ways (text matching, free/busy, MOVE, ETags,
scheduling), so run `caldav-mcp-probe` against yours and read
[docs/configuration.md](docs/configuration.md#verifying-against-a-new-server) first.
