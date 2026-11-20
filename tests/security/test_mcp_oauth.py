import asyncio
import json
import time
import traceback
from urllib.parse import parse_qs

import httpx
import pytest

from nanobot.config.schema import MCPServerConfig
from nanobot.security import mcp_oauth as oauth
from nanobot.security.secrets import is_sensitive_path, redact


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(oauth, "validate_url_target", lambda url: (True, ""))


@pytest.fixture
def cfg():
    return MCPServerConfig(url="https://mcp.example/mcp", oauth={"enabled": True})


async def seed(tmp_path, cfg, *, expired=True, refresh=True):
    store = oauth.OAuthStore("server", cfg, tmp_path)
    async with store.locked():
        await store.set_client_info(oauth.OAuthClientInformationFull(
            client_id="public-client", issuer="https://auth.example",
            token_endpoint_auth_method="none",
            redirect_uris=["http://127.0.0.1:8768/callback"],
        ))
        await store.set_tokens(oauth.OAuthToken(
            access_token="old-access-value-0123456789", token_type="Bearer", expires_in=3600,
            refresh_token="old-refresh-value-0123456789" if refresh else None,
        ))
        data = store.read()
        data["metadata"] = {"issuer": "https://auth.example", "token_endpoint": "https://auth.example/token"}
        if expired:
            data["expires_at"] = time.time() - 100
        store.write(data)
    return store


def new_tokens(refresh=True):
    result = {"access_token": "new-access-value-0123456789", "token_type": "Bearer", "expires_in": 3600}
    if refresh:
        result["refresh_token"] = "rotated-refresh-value-0123456789"
    return result


async def send(store, handler, method="GET"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                auth=oauth.StoredOAuthAuth(store, "server"),
                                follow_redirects=False) as client:
        return await client.request(method, store.resource)


async def test_private_atomic_storage_and_lifetime_survive_restart(tmp_path, cfg, monkeypatch):
    store = await seed(tmp_path, cfg, expired=False)
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert is_sensitive_path(store.path)
    original = store.read()["expires_at"]
    monkeypatch.setattr(oauth.time, "time", lambda: original - 12)
    reloaded = oauth.OAuthStore("server", cfg, tmp_path)
    assert (await reloaded.get_tokens()).expires_in == 12
    assert reloaded.read()["expires_at"] == original
    assert "access_token" not in json.dumps(reloaded.summary())
    assert "old-access-value" not in redact(json.dumps(reloaded.read()))


async def test_live_access_token_needs_no_refresh(tmp_path, cfg):
    store = await seed(tmp_path, cfg, expired=False)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer old-access-value-0123456789"
        return httpx.Response(200)

    assert (await send(store, handler)).status_code == 200
    assert calls == [cfg.url]


@pytest.mark.parametrize("include_refresh", [True, False])
async def test_refresh_rotation_and_omitted_refresh_token(tmp_path, cfg, include_refresh):
    store = await seed(tmp_path, cfg)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "auth.example":
            fields = parse_qs(request.content.decode())
            assert fields["grant_type"] == ["refresh_token"]
            assert fields["resource"] == [cfg.url]
            assert fields["refresh_token"] == ["old-refresh-value-0123456789"]
            assert "Authorization" not in request.headers
            return httpx.Response(200, json=new_tokens(include_refresh))
        assert request.headers["Authorization"] == "Bearer new-access-value-0123456789"
        return httpx.Response(200)

    await send(store, handler)
    assert len(calls) == 2
    expected = "rotated-refresh-value-0123456789" if include_refresh else "old-refresh-value-0123456789"
    assert store.read()["tokens"]["refresh_token"] == expected


async def test_early_401_refreshes_once_and_retries(tmp_path, cfg):
    store = await seed(tmp_path, cfg, expired=False)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "auth.example":
            return httpx.Response(200, json=new_tokens())
        return httpx.Response(401 if "old-access" in request.headers["Authorization"] else 200)

    assert (await send(store, handler)).status_code == 200
    assert calls == ["mcp.example", "auth.example", "mcp.example"]


async def test_shared_profile_serializes_concurrent_refresh(tmp_path, cfg):
    await seed(tmp_path, cfg)
    refreshes = 0

    async def handler(request):
        nonlocal refreshes
        if request.url.host == "auth.example":
            refreshes += 1
            await asyncio.sleep(0.03)
            return httpx.Response(200, json=new_tokens())
        assert request.headers["Authorization"] == "Bearer new-access-value-0123456789"
        return httpx.Response(200)

    results = await asyncio.gather(*(send(oauth.OAuthStore("server", cfg, tmp_path), handler) for _ in range(4)))
    assert all(r.status_code == 200 for r in results)
    assert refreshes == 1


@pytest.mark.parametrize("status", [429, 500, 503, 307])
async def test_temporary_or_redirect_refresh_failure_retains_credentials(tmp_path, cfg, status):
    store = await seed(tmp_path, cfg)
    before = store.path.read_bytes()
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(status, headers={"Location": "https://attacker.example/token"})

    with pytest.raises(oauth.MCPOAuthError, match="retained"):
        await send(store, handler)
    assert store.path.read_bytes() == before
    assert calls == ["auth.example"]


async def test_revoked_refresh_token_requires_login_without_repeated_refresh(tmp_path, cfg):
    store = await seed(tmp_path, cfg)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "private-marker"})

    for _ in range(2):
        with pytest.raises(oauth.MCPOAuthError, match="nanobot mcp login") as caught:
            await send(store, handler)
        assert "private-marker" not in str(caught.value)
    assert len(calls) == 1
    assert not store.summary()["logged_in"]


async def test_missing_refresh_token_requires_login_without_network(tmp_path, cfg):
    store = await seed(tmp_path, cfg, refresh=False)

    def handler(request):
        pytest.fail("No request may be sent when an expired token cannot be refreshed")

    with pytest.raises(oauth.MCPOAuthError, match="authorization required"):
        await send(store, handler)


async def test_retry_is_bounded_if_new_access_is_rejected(tmp_path, cfg):
    store = await seed(tmp_path, cfg, expired=False)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json=new_tokens()) if request.url.host == "auth.example" else httpx.Response(401)

    with pytest.raises(oauth.MCPOAuthError, match="authorization required"):
        await send(store, handler)
    assert calls == ["mcp.example", "auth.example", "mcp.example"]


async def test_cross_origin_auth_request_is_rejected(tmp_path, cfg):
    store = await seed(tmp_path, cfg, expired=False)
    async with httpx.AsyncClient(auth=oauth.StoredOAuthAuth(store, "server")) as client:
        with pytest.raises(oauth.MCPOAuthError, match="different origin"):
            await client.get("https://attacker.example/steal")


@pytest.mark.parametrize("target", ["root", "directory", "file", "lock"])
async def test_credential_symlinks_are_rejected(tmp_path, cfg, target):
    store = await seed(tmp_path, cfg)
    other = tmp_path / "public"
    other.mkdir()
    if target == "file":
        store.path.unlink()
        store.path.symlink_to(other / "stolen")
    elif target == "lock":
        lock = store.path.with_suffix(".lock")
        lock.unlink(missing_ok=True)
        lock.symlink_to(other / "stolen")
    else:
        folder = store.root if target == "root" else store.path.parent
        folder.rename(folder.with_name(folder.name + "-old"))
        folder.symlink_to(other, target_is_directory=True)
    with pytest.raises(oauth.MCPOAuthError, match="symbolic links|symbolic link"):
        async with store.locked():
            store.read()


async def test_unsafe_file_permissions_are_rejected(tmp_path, cfg):
    store = await seed(tmp_path, cfg)
    store.path.chmod(0o644)
    with pytest.raises(oauth.MCPOAuthError, match="0600"):
        store.read()


async def test_changed_resource_or_profile_cannot_read_other_credentials(tmp_path, cfg):
    first = await seed(tmp_path, cfg)
    cfg.oauth.profile = "another-account"
    second = oauth.OAuthStore("server", cfg, tmp_path)
    assert first.path != second.path
    assert not second.summary()["logged_in"]


def test_unsafe_discovery_url_is_blocked(monkeypatch):
    monkeypatch.setattr(oauth, "validate_url_target", lambda url: (False, "private address"))
    with pytest.raises(oauth.MCPOAuthError, match="safe HTTPS"):
        oauth.check_oauth_url("https://169.254.169.254/credentials")


async def test_invalid_token_response_is_sanitized_even_in_tracebacks(tmp_path, cfg):
    store = await seed(tmp_path, cfg)
    before = store.path.read_bytes()

    def handler(request):
        return httpx.Response(200, json={"access_token": "private-response-secret", "expires_in": "invalid"})

    with pytest.raises(oauth.MCPOAuthError, match="invalid response") as caught:
        await send(store, handler)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "private-response-secret" not in rendered
    assert store.path.read_bytes() == before


async def test_malformed_stored_token_is_sanitized(tmp_path, cfg):
    store = await seed(tmp_path, cfg)
    async with store.locked():
        data = store.read()
        data["tokens"] = {"access_token": "private-stored-secret", "expires_in": "invalid"}
        store.write(data)
    with pytest.raises(oauth.MCPOAuthError) as caught:
        await store.get_tokens()
    assert "private-stored-secret" not in "".join(traceback.format_exception(caught.value))
