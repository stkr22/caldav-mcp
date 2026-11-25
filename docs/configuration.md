# Configuration

All configuration is environment variables, read once at startup and validated. A bad value stops the server with a
message naming the variable; nothing falls back silently. The CalDAV password, username and API key are removed from
the process environment as soon as they are read.

## Required

| Variable | |
|---|---|
| `CALDAV_URL` | Calendar home or principal URL. `http://` is refused unless the host is loopback (or `CALDAV_ALLOW_PLAINTEXT=true`). No credentials, query string or fragment in it. |
| `CALDAV_USERNAME`, `CALDAV_PASSWORD` | The account. Most hosted services want an app-specific password. |
| `CALDAV_MCP_API_KEY` | At least 16 characters. Required unless `CALDAV_MCP_ALLOW_NO_AUTH=true`. |

## Which tools exist

| Variable | Default | |
|---|---|---|
| `CALDAV_ALLOW_TOOLS` | all | Exactly one of: comma-separated names; one prefix ending in `*`; the keyword `essential`. |
| `CALDAV_DENY_TOOLS` | none | Same forms; subtracted from the allow list. |
| `CALDAV_MCP_READ_ONLY` | `false` | `1`, `true` or `yes` (any case) hides every write tool. Applied last; it always wins. |

Names may be written with or without the `caldav_` prefix. An unknown name, a pattern that matches nothing, a
mixed form (`caldav_get_*,caldav_x`) or a set-but-empty variable stops the server: a typo in a *deny* list would
otherwise expose the tool it was meant to hide. Naming a write tool while read-only is on logs a warning (the tool
is known; read-only is suppressing it). `essential` is `caldav_list_calendars, caldav_get_events,
caldav_search_events, caldav_get_event_by_uid, caldav_get_freebusy, caldav_create_event, caldav_update_event`.

The eleven tools: `caldav_list_calendars`, `caldav_get_events`, `caldav_search_events`, `caldav_get_event_by_uid`,
`caldav_get_freebusy`, `caldav_create_event`, `caldav_update_event`, `caldav_delete_event`, `caldav_move_event`,
`caldav_add_attendee`, `caldav_remove_attendee`.

## Behaviour

| Variable | Default | |
|---|---|---|
| `TZ` | `UTC` | IANA zone (`Europe/London`). Decides what "today" and "this week" mean and how date-only values are read. An unknown zone is a startup error. |
| `CALDAV_WEEK_START` | `monday` | First day of the week for `this_week`, `next_week`, `last_week`. |
| `CALDAV_DEFAULT_CALENDAR` | none | Calendar (name or id) new events go to when the caller names none. |
| `CALDAV_MAX_EVENTS` | `50` | Default `limit` for lists; at most 500. |
| `CALDAV_TIMEOUT_SECONDS` | `20` | How long to wait for the CalDAV server. |

## HTTP server

| Variable | Default | |
|---|---|---|
| `CALDAV_MCP_PORT` | `8080` | |
| `CALDAV_MCP_PATH` | `/mcp` | Not `/health`, which is reserved for the unauthenticated probe route. |
| `CALDAV_MCP_TLS_CERT`, `CALDAV_MCP_TLS_KEY` | none | Built-in TLS for the MCP endpoint; both or neither. |
| `CALDAV_MCP_TLS_CA_BUNDLE` | none | Require client certificates signed by this CA. |
| `CALDAV_MCP_TRUST_PROXY` | `false` | Exactly `true`: behind one proxy that sets `X-Forwarded-For`, rate-limit by its rightmost entry. |
| `CALDAV_MCP_RATE_LIMIT_MAX_FAILURES` | `10` | Failed key attempts per address before it is blocked (with growing back-off). |
| `CALDAV_MCP_RATE_LIMIT_WINDOW_SECONDS` | `60` | |

`GET /health` returns `ok` with no key. Everything else needs the key, including `initialize` and `tools/list`.

## Settings that lower protection

Read strictly: only the exact spelling has effect, and an unrecognised value stops the server.

| Variable | |
|---|---|
| `CALDAV_MCP_ALLOW_NO_AUTH=true` | Run without an API key. Logs a warning at startup. |
| `CALDAV_MCP_CALDAV_VERIFY_SSL=false` | Do not verify the CalDAV server's certificate (default `true`; only `true`/`false`/`1`/`0`/`yes`/`no` are accepted, so a typo cannot disable it). |
| `CALDAV_ALLOW_PLAINTEXT=true` | Allow `http://` to a non-loopback CalDAV host. |

## Logging

One JSON line per tool call (tool name, outcome, duration) and per rejected request (address, reason), on stderr.
Never logged: the API key, credentials, queries, or anything from an event.

## Verifying against a new server

Servers differ in ways that change answers. Run the probe (read-only; prints only capability facts, never an event,
a calendar name or the URL) before relying on a server:

```bash
CALDAV_URL=... CALDAV_USERNAME=... CALDAV_PASSWORD=... caldav-mcp-probe        # add --json for a machine-readable form
```

| Fact | Why it matters |
|---|---|
| `scheduling_advertised` | If true, adding an attendee may make the server email an invitation. Keep the attendee tools off unless you want that. |
| `results_carry_etags`, `etag_header_on_resource` | Without ETags, writes are unconditional and say so. |
| `text_match_folds_ascii_case` | If false, server-side text search would miss differently-cased text; this server always re-checks locally, but it narrows less. |
| `server_side_expand_status`, `free_busy_query_status` | Informational: recurrences are expanded by this server and free/busy is computed from events, so neither is required. |
| `move_allowed_on_resource` | If false, `caldav_move_event` copies then deletes. |
| `time_range_query_status` | Must be 207; otherwise nothing will work. |
