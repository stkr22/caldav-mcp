# Contributing

```bash
uv sync --dev
uv run ruff check . && uv run ruff format --check .
uv run ty check
uv run pytest tests/unit                 # fast, no network
uv run pytest tests/integration          # needs a CalDAV server, see below
```

## Integration tests

They run against a real server (Radicale) because most of what matters here - recurrence, ETags, text matching,
MOVE - is behaviour of the protocol, not of our code.

* Dev container: the `radicale` service in `.devcontainer/compose.yml` listens on `127.0.0.1:5232`
  (account in `tests/integration/radicale/users`).
* Without Docker: `RADICALE_PORT=5233 tests/integration/radicale-local.sh`, then
  `CALDAV_TEST_URL=http://127.0.0.1:5233 uv run pytest tests/integration`.
* Nothing answering skips the suite locally; `CALDAV_REQUIRE_INTEGRATION=1` (CI) makes that a failure.

The clock is fixed at Wednesday 2026-11-25 15:30 Europe/Berlin, so every relative date in a test is explicit.
`test_scale.py` is slow (it seeds 1,500 events).

## Rules

* Everything an agent can send is validated in `inputs.py`; everything third-party that goes out passes through
  `sanitize.py`. New output fields follow the same rule.
* A tool change needs: a docstring an agent can act on, a test through the real MCP client, and an update to
  `docs/tools.md`. Tool names are part of deployments' allow-lists: renaming is a breaking change.
* Add a tool to `catalogue.py` (the test that compares it with what is registered will tell you if you forget).
* Never print event content, credentials or the API key in logs, errors or the probe.
* Wall-clock vs elapsed time: subtracting two datetimes with the same `tzinfo` ignores DST. Go through UTC when
  you mean elapsed time.
* Lint (`ruff`, line length 120) and types (`ty`) must pass; suppress a rule only on the line, with the reason.

## Releasing

A pushed `v*` tag builds the `Containerfile` for `linux/amd64` and `linux/arm64` and publishes it
(`.github/workflows/container-push.yml`).
