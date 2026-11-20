from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from loguru import logger

from nanobot.agent.tools import mcp as integration
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config import loader
from nanobot.config.schema import MCPServerConfig
from nanobot.security import mcp_oauth as oauth


@pytest.mark.parametrize("mode", ["sse", "streamableHttp"])
async def test_http_transport_uses_oauth_without_stale_headers(tmp_path, monkeypatch, mode):
    import mcp
    import mcp.client.sse
    import mcp.client.streamable_http

    monkeypatch.setattr(loader, "_current_config_path", tmp_path / "config.json")
    monkeypatch.setattr(integration, "validate_url_target", lambda url: (True, ""))
    monkeypatch.setattr(oauth, "validate_url_target", lambda url: (True, ""))
    cfg = MCPServerConfig(type=mode, url="https://mcp.example/mcp", enabled_tools=["selected"],
                          headers={"authorization": "Bearer stale-static", "X-Client": "nanobot"},
                          oauth={"enabled": True})
    store = oauth.OAuthStore("remote", cfg)
    async with store.locked():
        await store.set_tokens(oauth.OAuthToken(access_token="valid-oauth-access", token_type="Bearer", expires_in=600))
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer valid-oauth-access"
        assert request.headers["X-Client"] == "nanobot"
        return httpx.Response(200)

    monkeypatch.setattr(integration, "_pinned_transport_kwargs", lambda: {"transport": httpx.MockTransport(handler)})

    async def probe(url):
        return True

    monkeypatch.setattr(integration, "_probe_http_url", probe)

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            pass

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    monkeypatch.setattr(mcp, "ClientSession", Session)

    @asynccontextmanager
    async def sse(url, httpx_client_factory):
        async with httpx_client_factory() as client:
            assert not client.follow_redirects
            await client.get(url)
            yield object(), object()

    @asynccontextmanager
    async def stream(url, http_client):
        assert not http_client.follow_redirects
        await http_client.get(url)
        yield object(), object(), object()

    monkeypatch.setattr(mcp.client.sse, "sse_client", sse)
    monkeypatch.setattr(mcp.client.streamable_http, "streamable_http_client", stream)
    connected = await integration.connect_mcp_servers({"remote": cfg}, ToolRegistry())
    assert list(connected) == ["remote"]
    assert len(seen) == 1
    await connected["remote"].aclose()


async def test_missing_login_is_reported_before_constructing_transport(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "_current_config_path", tmp_path / "config.json")
    monkeypatch.setattr(integration, "validate_url_target", lambda url: (True, ""))
    cfg = MCPServerConfig(url="https://mcp.example/mcp", oauth={"enabled": True})

    async def probe(url):
        pytest.fail("Missing credentials must not trigger a network probe or background registration")

    monkeypatch.setattr(integration, "_probe_http_url", probe)
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        connected = await integration.connect_mcp_servers({"remote": cfg}, ToolRegistry())
    finally:
        logger.remove(sink)
    assert connected == {}
    assert "nanobot mcp login remote" in "".join(messages)
