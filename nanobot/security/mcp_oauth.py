"""Opt-in MCP OAuth credentials and noninteractive refresh for HTTP transports."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from filelock import AsyncFileLock
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url

from nanobot.config.paths import get_config_path
from nanobot.config.schema import MCPServerConfig
from nanobot.security.network import validate_url_target
from nanobot.security.secrets import register_secrets


class MCPOAuthError(RuntimeError):
    """Safe-to-display error; never includes token values or server error bodies."""


def check_oauth_url(url: str) -> None:
    parsed = urlsplit(url)
    ok, _ = validate_url_target(url)
    if not ok or parsed.scheme != "https" or parsed.username or parsed.fragment:
        raise MCPOAuthError("OAuth requires a safe HTTPS endpoint.")


async def validate_oauth_request(request: httpx.Request) -> None:
    # Includes discovery, registration and token requests, not only MCP traffic.
    check_oauth_url(str(request.url))


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise MCPOAuthError("MCP credential directories cannot be symbolic links.")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise MCPOAuthError("Invalid MCP credential directory.")
    path.chmod(0o700)


class OAuthStore:
    """Atomic owner-only storage, shared by matching profiles in one config directory."""

    def __init__(self, name: str, cfg: MCPServerConfig, data_dir: Path | None = None):
        self.resource = cfg.url
        key = hashlib.sha256(json.dumps([
            cfg.url, cfg.oauth.profile or name, cfg.oauth.scope,
            cfg.oauth.client_id, cfg.oauth.issuer, cfg.oauth.client_metadata_url,
        ], separators=(",", ":")).encode()).hexdigest()
        self.root = (data_dir or get_config_path().parent) / ".secrets" / "mcp-oauth"
        self.path = self.root / key / "auth.json"
        self.context: Any = None

    @asynccontextmanager
    async def locked(self, timeout: float = 15):
        self.root.parent.parent.mkdir(parents=True, exist_ok=True)
        for directory in (self.root.parent, self.root, self.path.parent):
            _private_directory(directory)
        lock = self.path.with_suffix(".lock")
        if lock.is_symlink():
            raise MCPOAuthError("MCP credential lock cannot be a symbolic link.")
        async with AsyncFileLock(lock, timeout=timeout, mode=0o600):
            yield

    def read(self) -> dict:
        for path in (self.root.parent, self.root, self.path.parent, self.path):
            if path.is_symlink():
                raise MCPOAuthError("MCP credential paths cannot be symbolic links.")
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {"resource": self.resource}
        except OSError as exc:
            raise MCPOAuthError("MCP credential file cannot be opened safely.") from exc
        with os.fdopen(descriptor) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and (
                info.st_mode & 0o077 or info.st_uid != os.getuid()
            )):
                raise MCPOAuthError("MCP credential file must be owned by this user and private (0600).")
            try:
                data = json.load(handle)
            except (ValueError, UnicodeError) as exc:
                raise MCPOAuthError("MCP credential file is invalid; log in again.") from exc
        if not isinstance(data, dict) or data.get("resource") != self.resource:
            raise MCPOAuthError("MCP credentials do not match the configured resource.")
        register_secrets(data)
        return data

    def write(self, data: dict) -> None:
        if self.path.is_symlink():
            raise MCPOAuthError("MCP credential file cannot be a symbolic link.")
        data = {**data, "resource": self.resource}
        register_secrets(data)
        descriptor, temporary = tempfile.mkstemp(prefix=".auth-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def get_tokens(self) -> OAuthToken | None:
        data = self.read()
        if not data.get("tokens"):
            return None
        try:
            token = OAuthToken.model_validate(data["tokens"])
        except ValueError:
            raise MCPOAuthError("Stored MCP token is invalid; log in again.") from None
        if data.get("expires_at") is not None:
            token.expires_in = max(0, int(data["expires_at"] - time.time()))
        return token

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self.read()
        data.update(tokens=tokens.model_dump(mode="json"),
                    expires_at=time.time() + tokens.expires_in if tokens.expires_in is not None else None)
        if self.context is not None:
            ctx = self.context
            data.update(
                metadata=ctx.oauth_metadata.model_dump(mode="json") if ctx.oauth_metadata else None,
                auth_server=ctx.auth_server_url,
                resource_metadata=(ctx.protected_resource_metadata.model_dump(mode="json")
                                   if ctx.protected_resource_metadata else None),
            )
        self.write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self.read().get("client")
        try:
            return OAuthClientInformationFull.model_validate(data) if data else None
        except ValueError:
            raise MCPOAuthError("Stored MCP client registration is invalid; log in again.") from None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self.read()
        data["client"] = client_info.model_dump(mode="json")
        self.write(data)

    def summary(self) -> dict:
        data = self.read()
        token = data.get("tokens") or {}
        expiry = data.get("expires_at")
        return {"logged_in": bool(token.get("access_token")),
                "refresh_available": bool(token.get("refresh_token")),
                "expires_at": expiry,
                "expired": expiry is not None and expiry <= time.time()}


class StoredOAuthAuth(httpx.Auth):
    """Refresh only; background agents never launch browsers or register OAuth clients."""

    def __init__(self, store: OAuthStore, server_name: str):
        self.store = store
        self.login_message = f"MCP OAuth authorization required. Run: nanobot mcp login {server_name} --config <config-path>"

    async def _token_flow(self, rejected: str | None):
        async with self.store.locked():
            data = self.store.read()
            if not data.get("tokens"):
                raise MCPOAuthError(self.login_message)
            try:
                token = OAuthToken.model_validate(data["tokens"])
            except ValueError:
                raise MCPOAuthError(self.login_message) from None
            expiry = data.get("expires_at")
            expiry_margin = 30 if token.refresh_token else 0
            expired = expiry is not None and expiry <= time.time() + expiry_margin
            if not expired and token.access_token != rejected:
                yield token.access_token
                return
            metadata, client = data.get("metadata") or {}, data.get("client") or {}
            endpoint = metadata.get("token_endpoint")
            issuer = metadata.get("issuer")
            if (not token.refresh_token or not endpoint or not client.get("client_id")
                    or not issuer or client.get("issuer") != issuer):
                raise MCPOAuthError(self.login_message)
            check_oauth_url(endpoint)
            resource = resource_url_from_server_url(self.store.resource)
            prm_resource = (data.get("resource_metadata") or {}).get("resource")
            if prm_resource and check_resource_allowed(requested_resource=resource, configured_resource=prm_resource):
                resource = prm_resource
            fields = {"grant_type": "refresh_token", "refresh_token": token.refresh_token,
                      "client_id": client["client_id"], "resource": resource}
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            method, secret = client.get("token_endpoint_auth_method"), client.get("client_secret")
            if method == "client_secret_basic" and secret:
                encoded = base64.b64encode((quote(client["client_id"], safe="") + ":" +
                                            quote(secret, safe="")).encode()).decode()
                headers["Authorization"] = "Basic " + encoded
            elif method == "client_secret_post" and secret:
                fields["client_secret"] = secret
            elif method not in (None, "none"):
                raise MCPOAuthError(self.login_message)
            response = yield httpx.Request("POST", endpoint, data=fields, headers=headers)
            await response.aread()
            if response.status_code in (400, 401):
                data.pop("tokens", None)
                data.pop("expires_at", None)
                self.store.write(data)
                raise MCPOAuthError(self.login_message)
            if response.status_code != 200:
                raise MCPOAuthError("MCP token refresh temporarily failed; credentials were retained.")
            try:
                updated = OAuthToken.model_validate_json(response.content)
                if not updated.access_token or updated.token_type.lower() != "bearer":
                    raise ValueError("Unsupported token response")
            except ValueError:
                raise MCPOAuthError("MCP token refresh returned an invalid response.") from None
            if not updated.refresh_token:
                updated.refresh_token = token.refresh_token
            await self.store.set_tokens(updated)
            yield updated.access_token

    async def async_auth_flow(self, request: httpx.Request):
        resource = httpx.URL(self.store.resource)
        if (request.url.scheme, request.url.host, request.url.port) != (resource.scheme, resource.host, resource.port):
            raise MCPOAuthError("Refusing to send MCP credentials to a different origin.")
        check_oauth_url(str(request.url))
        rejected = None
        for attempt in range(2):
            flow = self._token_flow(rejected)
            try:
                item = await anext(flow)
                while isinstance(item, httpx.Request):
                    response = yield item
                    item = await flow.asend(response)
                access_token = item
            finally:
                await flow.aclose()
            request.headers["Authorization"] = "Bearer " + access_token
            response = yield request
            if response.status_code != 401:
                return
            await response.aread()
            if attempt:
                raise MCPOAuthError(self.login_message)
            rejected = access_token
