"""Call an ASGI app in-process, without a web client library."""

import asyncio
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp


def request(
    app: ASGIApp,
    method: str = "GET",
    path: str = "/",
    headers: dict[str, str] | None = None,
    client: tuple[str, int] = ("203.0.113.9", 4000),
) -> tuple[int, dict[str, str], bytes]:
    """Send one request and return ``(status, headers, body)``."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
        "server": ("test", 80),
        "scheme": "http",
    }
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, body
