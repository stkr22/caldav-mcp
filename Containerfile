# Build stage: Python 3.15.2-trixie
FROM docker.io/library/python:3.15.2-trixie@sha256:b856c51ddffa0dfb3a25c7351abd0218857495922cf37352959b17ee03c0c938 AS build-python

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

# Install uv.
COPY --from=ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f /uv /uvx /bin/

WORKDIR /app

# Dependencies first, so this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache \
    uv sync --locked --no-dev --no-install-project

# Then the project itself, installed (not editable) into the same virtualenv.
COPY caldav_mcp/ caldav_mcp/
RUN --mount=type=cache,target=/root/.cache \
    uv sync --locked --no-dev --no-editable

# Runtime stage: Python 3.15.2-slim-trixie
FROM docker.io/library/python:3.15.2-slim-trixie@sha256:63bb298d2b7b88f176f51b5189f54ce6535c997682ea70885d2132f5d473a8a5

ENV PYTHONUNBUFFERED=1

# Create non-root user
RUN addgroup --system --gid 1001 appuser && adduser --system --uid 1001 --no-create-home --ingroup appuser appuser

WORKDIR /app
COPY --from=build-python /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"
USER appuser

# Streamable HTTP on 8080 (CALDAV_MCP_PORT); GET /health answers "ok" without a key.
EXPOSE 8080

ENTRYPOINT ["caldav-mcp"]
