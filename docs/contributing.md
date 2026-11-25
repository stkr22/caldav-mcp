# Contributing Guide

## Development Setup

1. Clone the repository:
   ```bash
   git clone <repo-url>
   cd caldav-mcp
   ```

2. Install [uv](https://docs.astral.sh/uv/) and sync dependencies:
   ```bash
   uv sync --dev
   ```

3. Copy `.env.example` to `.env` and configure your CalDAV credentials.

## Running Checks

```bash
uv run ruff check .             # lint
uv run ruff format --check .     # format check
uv run ty check                  # type check
uv run pytest tests/unit         # unit tests
```

## Code Style

- **Linting**: Ruff with the rule set in `pyproject.toml`. Line length 120.
- **Type checking**: ty (configured under `[tool.ty]` in `pyproject.toml`).
- **Docstrings**: Google or NumPy style. All public functions must have docstrings.
- **Comments**: Explain *why*, not *what*. Add inline comments for non-obvious logic.

## Testing

- Tests live in `tests/`.
- Use the fakes in `tests/conftest.py` (`FakeClient`, `FakeCalendar`, `FakeEvent`) to avoid network calls.
- Patch shared state via `mock.patch.object(server, "<name>")` — this is how the existing test suite works.
- Run the full suite before submitting: the checks above.

## Architecture Rules

1. **No new circular imports** — import from source modules, not from `server.py`.
2. **All tool handlers return `ToolResult`** — never return raw strings.
3. **Route shared state through `server.*`** in tool handlers so tests can patch it.
4. **Cache clients via `client_cache`** — never create a new `DAVClient` per call.
