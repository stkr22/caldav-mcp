"""The assembled server: which tools exist, and how it behaves over real HTTP."""

import asyncio
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from caldav_mcp.app import create_server, middleware_for
from caldav_mcp.catalogue import ALL_TOOLS, READ_TOOLS
from caldav_mcp.config import Config, load_config

KEY = "unit-test-key-0123456789"


def make_config(**extra: str) -> Config:
    env = {
        "CALDAV_URL": "https://dav.example.net/",
        "CALDAV_USERNAME": "u",
        "CALDAV_PASSWORD": "p",
        "CALDAV_MCP_API_KEY": KEY,
        **extra,
    }
    return load_config(env)


def registered(config: Config) -> set[str]:
    async def names() -> set[str]:
        async with Client(create_server(config)) as client:
            return {tool.name for tool in await client.list_tools()}

    return asyncio.run(names())


class TestRegistration:
    def test_default_registers_every_implemented_tool(self):
        assert registered(make_config()) <= set(ALL_TOOLS)
        assert set(READ_TOOLS) <= registered(make_config())

    def test_read_only_registers_no_write_tool(self):
        assert registered(make_config(CALDAV_MCP_READ_ONLY="1")) == set(READ_TOOLS)

    def test_allow_list_registers_exactly_that(self):
        assert registered(make_config(CALDAV_ALLOW_TOOLS="caldav_get_events,search_events")) == {
            "caldav_get_events",
            "caldav_search_events",
        }

    def test_deny_list_removes(self):
        assert "caldav_get_freebusy" not in registered(make_config(CALDAV_DENY_TOOLS="get_freebusy"))

    def test_every_read_tool_is_marked_read_only_and_described(self):
        async def tools():
            async with Client(create_server(make_config())) as client:
                return await client.list_tools()

        for tool in asyncio.run(tools()):
            assert tool.annotations is not None
            if tool.name in READ_TOOLS:
                assert tool.annotations.read_only_hint is True
            assert len(tool.description or "") > 40, f"{tool.name} needs a real description"
            assert "pro mode" not in (tool.description or "").lower()


@pytest.fixture
def live() -> Iterator[str]:
    """The real HTTP app with the real middleware, on a free port."""
    config = make_config()
    mcp = create_server(config)
    app = mcp.http_app(path="/mcp", middleware=middleware_for(config), transport="streamable-http", stateless_http=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def get(url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers or {}, data=data)  # noqa: S310 - test URL on loopback
    try:
        with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


class TestOverHttp:
    def test_health_answers_without_a_key(self, live):
        assert get(f"{live}/health") == (200, b"ok")

    def test_the_mcp_endpoint_refuses_everything_without_the_key(self, live):
        init = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        assert get(f"{live}/mcp", headers, init)[0] == 401
        assert get(f"{live}/mcp", {**headers, "x-api-key": "wrong"}, init)[0] == 401

    def test_with_the_key_a_client_can_list_tools(self, live):
        async def tools() -> set[str]:
            transport = StreamableHttpTransport(f"{live}/mcp", headers={"authorization": f"Bearer {KEY}"})
            async with Client(transport) as client:
                return {t.name for t in await client.list_tools()}

        assert set(READ_TOOLS) <= asyncio.run(tools())

    def test_without_the_key_not_even_tools_list_is_visible(self, live, caplog):
        async def attempt() -> None:
            async with Client(StreamableHttpTransport(f"{live}/mcp")) as client:
                await client.list_tools()

        with pytest.raises(Exception):  # noqa: B017 - the client's error text is generic; the audit log is the proof
            asyncio.run(attempt())
        assert "no key" in caplog.text

    def test_a_kubernetes_style_host_header_is_not_rejected(self, live):
        # A service reaches the pod as e.g. caldav-mcp.default.svc.cluster.local, not localhost.
        init = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}'
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "x-api-key": KEY,
            "host": "caldav-mcp.default.svc.cluster.local:8080",
        }
        code, body = get(f"{live}/mcp", headers, init)
        assert code == 200 and b"caldav-mcp" in body
