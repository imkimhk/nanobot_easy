"""Explicit browser authorization and local credential management for MCP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path

import typer
from rich.console import Console


def _server(name, config):
    from nanobot.config.loader import _resolve_env_vars, get_config_path, load_config
    from nanobot.config.schema import MCPServerConfig
    from nanobot.security.mcp_oauth import MCPOAuthError

    path = (config or get_config_path()).expanduser().resolve()
    if not path.is_file():
        raise MCPOAuthError("Configuration file does not exist.")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cfg = load_config(path)
    if cfg.tools.hardened_mode:
        raise MCPOAuthError("MCP is disabled by hardenedMode.")
    server = cfg.tools.mcp_servers.get(name)
    if server is None:
        raise MCPOAuthError("No MCP server with that name in this config.")
    raw = server.model_dump(by_alias=True)
    # OAuth replaces Authorization; an obsolete ${STATIC_TOKEN} must not block login.
    raw["headers"] = {k: v for k, v in raw["headers"].items() if k.lower() != "authorization"}
    server = MCPServerConfig.model_validate(_resolve_env_vars(raw))
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise MCPOAuthError("Config changed while it was being loaded; run the command again.")
    return path, server, digest


def _enable(path, name, digest):
    from nanobot.security.mcp_oauth import MCPOAuthError

    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise MCPOAuthError("Config changed during login. Enable oauth.enabled manually or run login again.")
    data = json.loads(raw)
    tools = data["tools"]
    servers = tools.get("mcpServers", tools.get("mcp_servers", {}))
    servers[name].setdefault("oauth", {})["enabled"] = True
    if "headers" in servers[name]:
        servers[name]["headers"] = {
            key: value for key, value in servers[name]["headers"].items()
            if key.lower() != "authorization"
        }
    descriptor, temporary = tempfile.mkstemp(prefix=".mcp-config-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise MCPOAuthError("Config changed during login; existing settings were preserved.")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def create_mcp_app(console: Console | None = None) -> typer.Typer:
    output = console or Console()
    app = typer.Typer(help="Manage HTTP MCP OAuth login, status and logout.")

    def report_error(exc):
        from nanobot.security.mcp_oauth import MCPOAuthError

        message = str(exc) if isinstance(exc, MCPOAuthError) else f"MCP OAuth failed ({type(exc).__name__})."
        output.print(message, markup=False)
        raise typer.Exit(1)

    @app.command("login")
    def login_command(
        server: str = typer.Argument(..., help="Configured MCP server name"),
        config: Path | None = typer.Option(None, "--config", "-c"),
        no_browser: bool = typer.Option(False, "--no-browser"),
        timeout: int = typer.Option(300, min=10, max=900),
    ):
        """Authorize in a browser, store credentials and enable OAuth for this server."""
        from nanobot.security.mcp_oauth_login import login

        try:
            path, cfg, digest = _server(server, config)
            result = asyncio.run(login(
                server, cfg, data_dir=path.parent, timeout=timeout, open_browser=not no_browser,
                notify=lambda text: output.print(text, markup=False),
            ))
            _enable(path, server, digest)
            output.print("MCP OAuth login complete. Reload MCP or restart this gateway.")
            if not result["refresh_available"]:
                output.print("This authorization did not include a refresh token; re-login will be needed on expiry.")
        except Exception as exc:
            report_error(exc)

    @app.command("status")
    def status_command(
        server: str = typer.Argument(...),
        config: Path | None = typer.Option(None, "--config", "-c"),
    ):
        """Show local credential state without displaying credentials or contacting the server."""
        from nanobot.security.mcp_oauth import OAuthStore

        try:
            path, cfg, _ = _server(server, config)
            result = OAuthStore(server, cfg, path.parent).summary()
            result["oauth_enabled"] = cfg.oauth.enabled
            output.print(json.dumps(result), markup=False)
        except Exception as exc:
            report_error(exc)

    @app.command("logout")
    def logout_command(
        server: str = typer.Argument(...),
        config: Path | None = typer.Option(None, "--config", "-c"),
    ):
        """Remove local credentials for this shared profile (does not revoke at the provider)."""
        from nanobot.security.mcp_oauth import OAuthStore

        try:
            path, cfg, _ = _server(server, config)
            store = OAuthStore(server, cfg, path.parent)

            async def clear():
                async with store.locked():
                    store.write({})

            asyncio.run(clear())
            output.print("Local MCP OAuth credentials removed for this shared profile.")
        except Exception as exc:
            report_error(exc)

    return app
