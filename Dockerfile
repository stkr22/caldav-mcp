# ── Stage 1: build dependencies ──────────────────────────────────
# Runtime uses 3.15 (matches requires-python); dependencies are resolved from uv.lock.
# Pin to exact patch tag for reproducible builds.
FROM python:3.15.2-alpine3.24 AS builder

WORKDIR /build

# Install build deps once; the wheel cache survives into stage 2 only if
# we copy it explicitly.
COPY --from=ghcr.io/astral-sh/uv:0.13.10 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o requirements.txt \
 && uv pip install --system --no-cache --prefix /install -r requirements.txt

# ── Stage 2: slim runtime ────────────────────────────────────────
FROM python:3.15.2-alpine3.24

RUN addgroup -S app && adduser -S app -G app

# Copy pre-built packages from the builder stage.
COPY --from=builder /install /usr/local

WORKDIR /app

COPY --chown=app:app server.py .
COPY --chown=app:app caldav_mcp/ ./caldav_mcp/

EXPOSE 8080

# TLS / HTTPS configuration (optional)
ENV CALDAV_MCP_TLS_CERT=""
ENV CALDAV_MCP_TLS_KEY=""
ENV CALDAV_MCP_TLS_CA_BUNDLE=""
# CalDAV server SSL verification (set to false only for testing with self-signed certs)
ENV CALDAV_MCP_CALDAV_VERIFY_SSL="true"
# Audit log format (text or json)
ENV CALDAV_MCP_LOG_FORMAT="text"

USER app

# Streamable HTTP endpoint on /mcp
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=10s \
  CMD python3 -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('127.0.0.1',8080)); s.close()"

CMD ["python3", "server.py"]
