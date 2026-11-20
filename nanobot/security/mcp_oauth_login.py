"""Interactive MCP OAuth authorization using the installed official MCP SDK."""

from __future__ import annotations

import asyncio
import logging
import secrets
import webbrowser
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata
from mcp.types import LATEST_PROTOCOL_VERSION

from nanobot.config.schema import MCPServerConfig
from nanobot.security.mcp_oauth import (
    MCPOAuthError,
    OAuthStore,
    check_oauth_url,
    validate_oauth_request,
)
from nanobot.security.network import PinnedDNSAsyncTransport, httpx_env_proxy_mounts


class _LoginCompleteError(Exception):
    """Stop the SDK flow after token exchange, before opening a long-lived SSE stream."""


class _LoginProvider(OAuthClientProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requested_scope = self.context.client_metadata.scope

    async def _perform_authorization_code_grant(self):
        # The SDK chooses the server's required scopes during discovery. Retain
        # explicit user-configured scopes as well (e.g. offline_access).
        scopes = (self.context.client_metadata.scope or "").split()
        scopes.extend((self.requested_scope or "").split())
        self.context.client_metadata.scope = " ".join(dict.fromkeys(scopes)) or None
        return await super()._perform_authorization_code_grant()

    async def _handle_token_response(self, response):
        await super()._handle_token_response(response)
        raise _LoginCompleteError


class LoopbackCallback:
    """One-shot loopback listener; invalid state cannot consume a pending login."""

    def __init__(self, port: int, timeout: float = 300):
        self.port, self.timeout = port, timeout
        self.uri = f"http://127.0.0.1:{port}/callback"
        self.state: str | None = None

    async def __aenter__(self):
        self.future = asyncio.get_running_loop().create_future()
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port, limit=8192)
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        if not self.future.done():
            self.future.cancel()
        elif not self.future.cancelled():
            self.future.exception()  # Consume a declined callback even if the network flow failed first.

    async def _handle(self, reader, writer):
        status, text = "400 Bad Request", "Invalid OAuth callback."
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = raw.decode("ascii").split("\r\n")
            method, target, _ = lines[0].split(" ")
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            host = next((v.strip() for k, v in headers.items() if k.lower() == "host"), "")
            url = urlsplit(target)
            query = parse_qs(url.query)
            states = query.get("state", [""])
            valid = (method == "GET" and not url.netloc and url.path == "/callback"
                     and host == f"127.0.0.1:{self.port}" and len(states) == 1 and self.state
                     and secrets.compare_digest(states[0], self.state))
            if valid and not self.future.done():
                if "error" in query:
                    self.future.set_exception(MCPOAuthError("MCP authorization was declined."))
                    text = "Authorization was declined. Return to nanobot."
                elif len(query.get("code", [])) == 1 and query["code"][0]:
                    self.future.set_result((query["code"][0], states[0]))
                    status, text = "200 OK", "Authorization received. You can close this window."
            elif self.future.done():
                status, text = "410 Gone", "This login request is already complete."
        except (OSError, ValueError, UnicodeError, asyncio.TimeoutError, asyncio.LimitOverrunError,
                asyncio.IncompleteReadError):
            pass
        body = text.encode()
        try:
            writer.write((f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
                          "Content-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\n"
                          "Referrer-Policy: no-referrer\r\nContent-Security-Policy: default-src 'none'\r\n"
                          "Connection: close\r\n\r\n").encode() + body)
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def wait(self):
        try:
            return await asyncio.wait_for(asyncio.shield(self.future), self.timeout)
        except asyncio.TimeoutError as exc:
            raise MCPOAuthError("MCP login timed out; run login again.") from exc


async def login(name: str, cfg: MCPServerConfig, *, data_dir=None, timeout=300,
                open_browser=True, notify=print) -> dict:
    check_oauth_url(cfg.url)
    if cfg.type == "stdio" or (not cfg.type and cfg.command):
        raise MCPOAuthError("OAuth is available only for HTTP/SSE MCP servers.")
    if cfg.oauth.client_id:
        if not cfg.oauth.issuer:
            raise MCPOAuthError("A pre-registered OAuth client requires oauth.issuer.")
        check_oauth_url(cfg.oauth.issuer)
    store = OAuthStore(name, cfg, data_dir)
    async with store.locked(timeout=timeout):
        async with LoopbackCallback(cfg.oauth.callback_port, timeout) as callback:
            async def redirect(url):
                check_oauth_url(url)
                callback.state = parse_qs(urlsplit(url).query).get("state", [None])[0]
                if not callback.state:
                    raise MCPOAuthError("Authorization URL did not include state.")
                notify("Complete MCP authorization in your browser.")
                if not open_browser or not webbrowser.open(url):
                    notify(url)

            metadata = OAuthClientMetadata(
                client_name="nanobot", redirect_uris=[callback.uri],
                grant_types=["authorization_code", "refresh_token"], response_types=["code"],
                token_endpoint_auth_method="client_secret_post" if cfg.oauth.client_secret else "none",
                scope=cfg.oauth.scope,
            )
            previous = store.read()
            data = {k: v for k, v in previous.items() if k not in {"tokens", "expires_at"}}
            if cfg.oauth.client_id:
                data["client"] = OAuthClientInformationFull(
                    **metadata.model_dump(), client_id=cfg.oauth.client_id,
                    client_secret=cfg.oauth.client_secret,
                    issuer=cfg.oauth.issuer,
                ).model_dump(mode="json")
            # A different callback/scope requires registration with matching metadata.
            elif data.get("client") and (
                data["client"].get("redirect_uris") != [callback.uri]
                or data["client"].get("scope") != cfg.oauth.scope
            ):
                data.pop("client", None)
            provider = _LoginProvider(
                cfg.url, metadata, store, redirect_handler=redirect,
                callback_handler=callback.wait, timeout=timeout,
                client_metadata_url=cfg.oauth.client_metadata_url,
            )
            store.context = provider.context
            # SDK exception formatting can include unvalidated response bodies. Display only
            # our sanitized errors; never send those exceptions or tokens to the agent context.
            sdk_logs = [logging.getLogger(n) for n in ("mcp.client.auth.oauth2", "mcp.client.auth.utils")]
            disabled = [log.disabled for log in sdk_logs]
            try:
                store.write(data)
                for log in sdk_logs:
                    log.disabled = True
                async with asyncio.timeout(timeout):
                    async with httpx.AsyncClient(
                        auth=provider, follow_redirects=False, timeout=httpx.Timeout(30, connect=10),
                        event_hooks={"request": [validate_oauth_request]},
                        transport=PinnedDNSAsyncTransport(), mounts=httpx_env_proxy_mounts(),
                        trust_env=False,
                    ) as client:
                        headers = {
                            "Accept": "application/json, text/event-stream",
                            "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
                            **{k: v for k, v in cfg.headers.items() if k.lower() != "authorization"},
                        }
                        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                            "protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
                            "clientInfo": {"name": "nanobot", "version": "0.2.2"}}}
                        is_sse = cfg.type == "sse" or (not cfg.type and cfg.url.rstrip('/').endswith('/sse'))
                        method = "GET" if is_sse else "POST"
                        kwargs = {} if is_sse else {"json": payload}
                        try:
                            await client.request(method, cfg.url, headers=headers, **kwargs)
                        except _LoginCompleteError:
                            pass
                        if not store.summary()["logged_in"]:
                            raise MCPOAuthError("MCP login did not establish an authenticated connection.")
                        # Verify acceptance without reading an indefinite SSE response body.
                        headers["Authorization"] = "Bearer " + store.read()["tokens"]["access_token"]
                        async with client.stream(method, cfg.url, headers=headers, auth=None, **kwargs) as response:
                            if response.status_code != 200:
                                raise MCPOAuthError("MCP server rejected the new authorization.")
            except BaseException:
                store.write(previous)
                raise
            finally:
                for log, value in zip(sdk_logs, disabled):
                    log.disabled = value
    return store.summary()
