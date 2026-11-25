# AGENTS.md - caldav-mcp

> **TL;DR**: An MCP server that gives an LLM agent compact, safe read/write access to one CalDAV account, over
> Streamable HTTP. 11 tools. Python 3.15, uv, FastMCP 4, python-caldav 3.7, icalendar 7.

## Goal

Make an agent doing everyday calendar work give fewer wrong or empty answers. The design measure is *no silent wrong
answers*: truncation, hidden items, failed calendars and empty-because-nothing-matched are all said out loud; ranges
are echoed; recurring events are never changed without an explicit scope; event text is treated as hostile.

Read `README.md` for the behaviour, `docs/tools.md` for the tool contract, `docs/architecture.md` for the layout,
`docs/security.md` for the threat model.

## Layout

```
caldav_mcp/   app, config, catalogue, toolfilter, httpauth, ratelimit, session, dates, events, filters,
              engine, inputs, series, ops, results, errors, sanitize, probe, tools/{read,write,attendees}
tests/unit/        no network
tests/integration/ real Radicale; fixed clock (Wed 2026-11-25 15:30 Europe/Berlin)
Containerfile      Debian trixie, uv sync into /app/.venv, non-root, ENTRYPOINT caldav-mcp
```

## Tools (11; 5 in read-only mode)

Read: `caldav_list_calendars`, `caldav_get_events`, `caldav_search_events`, `caldav_get_event_by_uid`,
`caldav_get_freebusy`. Write: `caldav_create_event`, `caldav_update_event`, `caldav_delete_event`,
`caldav_move_event`, `caldav_add_attendee`, `caldav_remove_attendee`.

## Configuration

Environment only; see `docs/configuration.md` and `.env.example`. Required: `CALDAV_URL`, `CALDAV_USERNAME`,
`CALDAV_PASSWORD`, `CALDAV_MCP_API_KEY` (>= 16 chars, unless `CALDAV_MCP_ALLOW_NO_AUTH=true`). Tool control:
`CALDAV_ALLOW_TOOLS`, `CALDAV_DENY_TOOLS`, `CALDAV_MCP_READ_ONLY`.

## Rules for changes

- Validate everything written in `inputs.py`; sanitise everything third-party that goes out via `sanitize.py`.
- Never log or print the API key, credentials, event content or calendar names (the probe included).
- Do not weaken: key required at startup, key checked before any MCP method, TLS verification default, read-only
  hiding write tools, strict parsing of switches that lower protection.
- Time: same-zone datetime subtraction is wall-clock; compute elapsed time through UTC. Day boundaries come from local
  dates, not from adding 24 hours.
- Tool names are baked into deployments' allow-lists; renaming one is a breaking change. Keep `catalogue.py` in step.
- A tool change needs a test through the MCP client (`tests/integration/conftest.py:Agent`) and a `docs/tools.md` update.

## Development

```bash
uv sync --dev
uv run ruff check . && uv run ruff format --check . && uv run ty check
uv run pytest tests/unit
uv run pytest tests/integration      # needs Radicale: dev-container service, or tests/integration/radicale-local.sh
```

## Git Branch Strategy

- The main branch is `main`.
- The testing branch is `testing`.
- The `main` branch is protected and cannot be pushed to directly.
- All development must happen in a separate branch from `main` and `testing`.
- After a task is finished, a pull request must be created targeting `testing`.
