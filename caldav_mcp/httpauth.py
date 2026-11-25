"""Inbound authentication: one shared API key, enforced before any MCP method runs.

A pure ASGI middleware rather than a check inside each tool, so ``initialize`` and
``tools/list`` need the key too, and a tool added later cannot forget to ask. Only
``/health`` is exempt - it answers ``ok`` and nothing else, so a probe works.

The key travels as ``Authorization: Bearer <key>`` or ``X-Api-Key: <key>``. It is
compared in constant time (both sides are hashed first, so length does not leak),
is never logged, and failures are rate-limited per client address. The address is
the TCP peer; ``X-Forwarded-For`` is read only with ``CALDAV_MCP_TRUST_PROXY=true``,
and then only its rightmost entry, which the trusted proxy wrote. Anything else
lets a caller pick its own bucket.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from starlette.types import ASGIApp, Receive, Scope, Send

from caldav_mcp.audit import log_auth_failure
from caldav_mcp.ratelimit import RateLimiter

HEALTH_PATH = "/health"


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def client_address(scope: Scope, *, trust_proxy: bool) -> str:
    """Return the key failures are counted under."""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    if trust_proxy and headers.get("x-forwarded-for"):
        return headers["x-forwarded-for"].split(",")[-1].strip() or "unknown"
    client = scope.get("client")
    return client[0] if client else "unknown"


def _presented_key(scope: Scope) -> str:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return headers.get("x-api-key", "").strip()


async def _reply(send: Send, status: int, body: dict[str, str], extra: list[tuple[bytes, bytes]] | None = None) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()), *(extra or [])]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class ApiKeyMiddleware:
    """Reject any HTTP request that does not carry the configured key."""

    def __init__(self, app: ASGIApp, *, api_key: str, limiter: RateLimiter, trust_proxy: bool = False) -> None:
        """Wrap *app*; only the digest of *api_key* is kept."""
        self.app = app
        self._expected = _digest(api_key)
        self._limiter = limiter
        self._trust_proxy = trust_proxy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on only when it carries the key (or is the health probe)."""
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return  # no WebSocket endpoint exists; drop the connection
        if scope["path"] == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        address = client_address(scope, trust_proxy=self._trust_proxy)
        wait = self._limiter.retry_after(address)
        if wait:
            log_auth_failure(address, f"rate limited ({wait}s)")
            await _reply(send, 429, {"error": "too many failed attempts"}, [(b"retry-after", str(wait).encode())])
            return
        presented = _presented_key(scope)
        if presented and hmac.compare_digest(_digest(presented), self._expected):
            self._limiter.reset(address)
            await self.app(scope, receive, send)
            return
        self._limiter.record_failure(address)
        log_auth_failure(address, "invalid key" if presented else "no key")
        await _reply(send, 401, {"error": "unauthorized"}, [(b"www-authenticate", b'Bearer realm="caldav-mcp"')])
