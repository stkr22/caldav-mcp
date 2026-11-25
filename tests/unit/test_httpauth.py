"""Inbound authentication: enforced before MCP, constant-time, rate-limited, never logged."""

import asyncio
import json
import logging

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.types import ASGIApp

from caldav_mcp.httpauth import ApiKeyMiddleware, client_address
from caldav_mcp.ratelimit import RateLimiter
from tests.asgi import request

KEY = "correct-horse-battery-staple"


def build(*, trust_proxy: bool = False, max_failures: int = 3) -> ASGIApp:
    async def mcp_route(_request):
        return PlainTextResponse("tool-list")

    async def health(_request):
        return PlainTextResponse("ok")

    return Starlette(
        routes=[Route("/mcp", mcp_route, methods=["GET", "POST"]), Route("/health", health)],
        middleware=[
            Middleware(ApiKeyMiddleware, api_key=KEY, limiter=RateLimiter(max_failures, 60), trust_proxy=trust_proxy)
        ],
    )


def status(app: ASGIApp, headers: dict[str, str] | None = None, path: str = "/mcp", **kw) -> int:
    return request(app, "POST", path, headers, **kw)[0]


class TestAuthentication:
    def test_no_key_is_rejected_before_anything_runs(self):
        code, headers, body = request(build(), "POST", "/mcp")
        assert code == 401 and headers["www-authenticate"].startswith("Bearer")
        assert b"tool-list" not in body

    @pytest.mark.parametrize(
        "headers",
        [{"authorization": f"Bearer {KEY}"}, {"authorization": f"bearer {KEY}"}, {"x-api-key": KEY}],
    )
    def test_either_header_form_is_accepted(self, headers):
        assert status(build(), headers) == 200

    @pytest.mark.parametrize(
        "headers",
        [
            {"authorization": "Bearer wrong"},
            {"authorization": f"Basic {KEY}"},
            {"authorization": f"Bearer {KEY}x"},
            {"authorization": f"Bearer {KEY[:-1]}"},
            {"x-api-key": ""},
            {"authorization": "Bearer "},
        ],
    )
    def test_wrong_or_malformed_keys_are_rejected(self, headers):
        assert status(build(), headers) == 401

    def test_health_is_open_and_says_only_ok(self):
        code, _, body = request(build(), "GET", "/health")
        assert (code, body) == (200, b"ok")

    def test_nothing_else_is_exempt(self):
        app = build()
        assert request(app, "GET", "/healthz")[0] == 401
        assert request(app, "GET", "/health/")[0] == 401
        assert request(app, "GET", "/")[0] == 401

    def test_websockets_are_dropped(self):
        sent: list[dict] = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        asyncio.run(build()({"type": "websocket", "path": "/mcp", "headers": []}, receive, send))
        assert sent == []


class TestRateLimiting:
    def test_repeated_failures_block_even_the_correct_key(self):
        app = build(max_failures=3)
        for _ in range(3):
            assert status(app, {"x-api-key": "bad"}) == 401
        code, headers, _ = request(app, "POST", "/mcp", {"x-api-key": KEY})
        assert code == 429 and int(headers["retry-after"]) >= 5

    def test_a_spoofed_forwarded_for_does_not_choose_a_new_bucket(self):
        app = build(max_failures=3)
        for i in range(3):
            status(app, {"x-forwarded-for": f"10.0.0.{i}", "x-api-key": "bad"})
        assert status(app, {"x-forwarded-for": "10.9.9.9", "x-api-key": "bad"}) == 429

    def test_other_peers_are_not_locked_out_by_one_attacker(self):
        app = build(max_failures=2)
        for _ in range(2):
            status(app, {"x-api-key": "bad"}, client=("198.51.100.1", 1))
        assert status(app, {"x-api-key": KEY}, client=("198.51.100.1", 1)) == 429
        assert status(app, {"x-api-key": KEY}, client=("192.0.2.77", 1)) == 200

    def test_behind_a_trusted_proxy_the_rightmost_entry_is_the_client(self):
        app = build(trust_proxy=True, max_failures=2)
        for _ in range(2):
            status(app, {"x-forwarded-for": "6.6.6.6, 10.0.0.1", "x-api-key": "bad"})
        assert status(app, {"x-forwarded-for": "7.7.7.7, 10.0.0.1", "x-api-key": KEY}) == 429
        assert status(app, {"x-forwarded-for": "7.7.7.7, 10.0.0.2", "x-api-key": KEY}) == 200

    def test_address_comes_from_the_peer_unless_the_proxy_is_trusted(self):
        scope = {"client": ("1.2.3.4", 5), "headers": [(b"x-forwarded-for", b"9.9.9.9")]}
        assert client_address(scope, trust_proxy=False) == "1.2.3.4"
        assert client_address(scope, trust_proxy=True) == "9.9.9.9"
        assert client_address({"headers": []}, trust_proxy=False) == "unknown"

    def test_success_clears_the_failure_count(self):
        app = build(max_failures=3)
        for _ in range(2):
            status(app, {"x-api-key": "bad"})
        assert status(app, {"x-api-key": KEY}) == 200
        for _ in range(2):
            assert status(app, {"x-api-key": "bad"}) == 401


def test_the_key_is_never_logged(caplog):
    app = build()
    with caplog.at_level(logging.INFO, logger="caldav-mcp.audit"):
        status(app, {"authorization": f"Bearer {KEY}-nope"})
        status(app, {"x-api-key": KEY})
    assert "nope" not in caplog.text and KEY not in caplog.text
    assert json.loads(caplog.records[0].message)["reason"] == "invalid key"
