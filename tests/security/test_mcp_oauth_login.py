import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from nanobot.config.schema import MCPServerConfig
from nanobot.security import mcp_oauth as oauth
from nanobot.security import mcp_oauth_login as interactive


async def callback_request(port, query, host=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    target = "/callback?" + urlencode(query, doseq=True)
    writer.write(f"GET {target} HTTP/1.1\r\nHost: {host or f'127.0.0.1:{port}'}\r\n\r\n".encode())
    await writer.drain()
    result = await reader.read()
    writer.close()
    await writer.wait_closed()
    return result


async def test_callback_rejects_wrong_state_host_and_duplicates_then_accepts_once(unused_tcp_port):
    async with interactive.LoopbackCallback(unused_tcp_port, 1) as callback:
        callback.state = "correct-state"
        for query, host in [
            ({"code": "private-code", "state": "wrong"}, None),
            ({"code": "private-code", "state": "correct-state"}, "attacker.example"),
            ({"code": ["one", "two"], "state": "correct-state"}, None),
            ({"code": "private-code", "state": ["correct-state", "wrong"]}, None),
        ]:
            response = await callback_request(unused_tcp_port, query, host)
            assert response.startswith(b"HTTP/1.1 400")
            assert not callback.future.done()
        response = await callback_request(unused_tcp_port, {"code": "private-code", "state": "correct-state"})
        assert response.startswith(b"HTTP/1.1 200")
        assert b"private-code" not in response
        assert b"Cache-Control: no-store" in response
        assert await callback.wait() == ("private-code", "correct-state")
        assert (await callback_request(unused_tcp_port, {"code": "again", "state": "correct-state"})).startswith(b"HTTP/1.1 410")
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", unused_tcp_port)


async def test_declined_callback_and_timeout(unused_tcp_port):
    async with interactive.LoopbackCallback(unused_tcp_port, 0.01) as callback:
        with pytest.raises(oauth.MCPOAuthError, match="timed out"):
            await callback.wait()
    async with interactive.LoopbackCallback(unused_tcp_port, 1) as callback:
        callback.state = "correct"
        await callback_request(unused_tcp_port, {"error": "access_denied", "state": "correct"})
        with pytest.raises(oauth.MCPOAuthError, match="declined"):
            await callback.wait()


@pytest.mark.parametrize("transport", ["streamableHttp", "sse"])
async def test_real_sdk_pkce_discovery_registration_callback_and_persisted_refresh(
    tmp_path, unused_tcp_port, monkeypatch, transport,
):
    monkeypatch.setattr(oauth, "validate_url_target", lambda url: (True, ""))
    cfg = MCPServerConfig(type=transport, url="https://mcp.example/mcp",
                          headers={"Authorization": "Bearer obsolete-value", "X-Api-Key": "mcp-only-key"},
                          oauth={"enabled": True, "scope": "offline_access", "callbackPort": unused_tcp_port})
    authorize = {}
    callback_tasks = []
    calls = []
    initialized = 0

    def browser_open(url):
        authorize.update(parse_qs(urlsplit(url).query))
        redirect = urlsplit(authorize["redirect_uri"][0])
        callback_tasks.append(asyncio.create_task(callback_request(
            redirect.port, {"code": "private-code", "state": authorize["state"][0]},
        )))
        return True

    class NeverReadStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("Login must not try to consume a long-lived event stream")
            yield b""

    def handler(request):
        if request.url.host == "auth.example":
            assert "X-Api-Key" not in request.headers
        elif request.url.host == "mcp.example" and request.url.path == "/mcp":
            assert request.headers["X-Api-Key"] == "mcp-only-key"
        nonlocal initialized
        calls.append((request.method, request.url.host, request.url.path))
        if request.url.host == "mcp.example" and request.url.path == "/mcp":
            if request.headers.get("Authorization") != "Bearer live-access-secret-123456":
                assert "Authorization" not in request.headers
                return httpx.Response(401, headers={"WWW-Authenticate":
                    'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource"'})
            initialized += 1
            return httpx.Response(200, stream=NeverReadStream())
        if request.url.path == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"resource": cfg.url,
                "authorization_servers": ["https://auth.example/tenant"], "scopes_supported": ["default"]})
        if "oauth-authorization-server" in request.url.path:
            return httpx.Response(200, json={
                "issuer": "https://auth.example/tenant",
                "authorization_endpoint": "https://auth.example/tenant/authorize",
                "token_endpoint": "https://auth.example/tenant/token",
                "registration_endpoint": "https://auth.example/connect/register?tenant=one",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            })
        if request.url.path == "/connect/register":
            data = json.loads(request.content)
            assert "refresh_token" in data["grant_types"]
            return httpx.Response(201, json={**data, "client_id": "public-nanobot-client"})
        if request.url.path == "/tenant/token":
            values = parse_qs(request.content.decode())
            assert values["resource"] == [cfg.url]
            assert values["code"] == ["private-code"]
            assert authorize["code_challenge_method"] == ["S256"]
            assert set(authorize["scope"][0].split()) == {"default", "offline_access"}
            digest = hashlib.sha256(values["code_verifier"][0].encode()).digest()
            assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == authorize["code_challenge"][0]
            return httpx.Response(200, json={"access_token": "live-access-secret-123456", "token_type": "Bearer",
                "refresh_token": "live-refresh-secret-123456", "expires_in": 3600, "scope": "default offline_access"})
        pytest.fail(f"Unexpected mock endpoint: {request.url.path}")

    monkeypatch.setattr(interactive.webbrowser, "open", browser_open)
    monkeypatch.setattr(interactive, "PinnedDNSAsyncTransport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(interactive, "httpx_env_proxy_mounts", lambda: {})
    notices = []
    result = await interactive.login("server", cfg, data_dir=tmp_path, timeout=5, notify=notices.append)
    await asyncio.gather(*callback_tasks)
    assert result["logged_in"] and result["refresh_available"] and not result["expired"]
    assert initialized == 1
    assert "live-access" not in str(notices) and "private-code" not in str(notices)
    stored = oauth.OAuthStore("server", cfg, tmp_path).read()
    assert stored["metadata"]["token_endpoint"] == "https://auth.example/tenant/token"
    assert stored["client"]["issuer"] == "https://auth.example/tenant"
    assert stored["resource_metadata"]["resource"] == cfg.url
    assert cfg.headers["Authorization"] == "Bearer obsolete-value"


async def test_failed_login_restores_previous_credentials(tmp_path, unused_tcp_port, monkeypatch):
    monkeypatch.setattr(oauth, "validate_url_target", lambda url: (True, ""))
    cfg = MCPServerConfig(url="https://mcp.example/mcp", oauth={"callbackPort": unused_tcp_port})
    store = oauth.OAuthStore("server", cfg, tmp_path)
    async with store.locked():
        await store.set_tokens(oauth.OAuthToken(access_token="previous-value", token_type="Bearer", expires_in=600))
    previous = store.read()
    monkeypatch.setattr(interactive, "PinnedDNSAsyncTransport", lambda: httpx.MockTransport(lambda req: httpx.Response(503)))
    monkeypatch.setattr(interactive, "httpx_env_proxy_mounts", lambda: {})
    with pytest.raises(oauth.MCPOAuthError, match="did not establish"):
        await interactive.login("server", cfg, data_dir=tmp_path, timeout=5)
    assert store.read() == previous


async def test_preregistered_client_requires_explicit_issuer(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth, "validate_url_target", lambda url: (True, ""))
    cfg = MCPServerConfig(url="https://mcp.example/mcp", oauth={"clientId": "known-client"})
    with pytest.raises(oauth.MCPOAuthError, match="oauth.issuer"):
        await interactive.login("server", cfg, data_dir=tmp_path)


async def test_invalid_metadata_url_does_not_destroy_credentials(tmp_path, unused_tcp_port, monkeypatch):
    monkeypatch.setattr(oauth, "validate_url_target", lambda url: (True, ""))
    cfg = MCPServerConfig(url="https://mcp.example/mcp", oauth={
        "callbackPort": unused_tcp_port, "clientMetadataUrl": "http://invalid.example/client.json"})
    store = oauth.OAuthStore("server", cfg, tmp_path)
    async with store.locked():
        await store.set_tokens(oauth.OAuthToken(access_token="previous-value", token_type="Bearer", expires_in=600))
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="client_metadata_url"):
        await interactive.login("server", cfg, data_dir=tmp_path, timeout=5)
    assert store.path.read_bytes() == before
