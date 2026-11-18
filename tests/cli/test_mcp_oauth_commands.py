import json

import pytest
from typer.testing import CliRunner

from nanobot.cli.mcp import create_mcp_app
from nanobot.security import mcp_oauth_login


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "providers": {"openai": {"apiKey": "${UNRELATED_NOT_SET}"}},
        "tools": {"mcpServers": {"remote": {
            "url": "https://mcp.example/mcp",
            "headers": {"Authorization": "${OBSOLETE_TOKEN_NOT_SET}", "X-Client": "nanobot"},
        }}},
    }))
    return path


def test_login_enables_only_selected_oauth_and_preserves_unresolved_config(config_path, monkeypatch):
    before = json.loads(config_path.read_text())
    observed = []

    async def fake_login(name, cfg, **kwargs):
        observed.append((name, cfg))
        return {"logged_in": True, "refresh_available": True}

    monkeypatch.setattr(mcp_oauth_login, "login", fake_login)
    result = CliRunner().invoke(create_mcp_app(), ["login", "remote", "--config", str(config_path), "--no-browser"])
    assert result.exit_code == 0, result.output
    after = json.loads(config_path.read_text())
    before["tools"]["mcpServers"]["remote"]["oauth"] = {"enabled": True}
    before["tools"]["mcpServers"]["remote"]["headers"].pop("Authorization")
    assert after == before
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert observed[0][1].headers == {"X-Client": "nanobot"}
    assert "OBSOLETE_TOKEN" not in result.output


def test_login_failure_preserves_config_and_does_not_print_sensitive_exception(config_path, monkeypatch):
    before = config_path.read_bytes()

    async def fake_login(*args, **kwargs):
        raise ValueError("private-access-token-in-response")

    monkeypatch.setattr(mcp_oauth_login, "login", fake_login)
    result = CliRunner().invoke(create_mcp_app(), ["login", "remote", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "private-access" not in result.output
    assert config_path.read_bytes() == before


def test_concurrent_config_change_is_preserved(config_path, monkeypatch):
    async def fake_login(*args, **kwargs):
        data = json.loads(config_path.read_text())
        data["tools"]["mcpServers"]["remote"]["url"] = "https://other.example/mcp"
        config_path.write_text(json.dumps(data))
        return {"logged_in": True, "refresh_available": True}

    monkeypatch.setattr(mcp_oauth_login, "login", fake_login)
    result = CliRunner().invoke(create_mcp_app(), ["login", "remote", "--config", str(config_path)])
    assert result.exit_code == 1
    assert json.loads(config_path.read_text())["tools"]["mcpServers"]["remote"]["url"] == "https://other.example/mcp"
    assert "oauth" not in json.loads(config_path.read_text())["tools"]["mcpServers"]["remote"]


def test_status_and_logout_without_old_static_token(config_path):
    runner = CliRunner()
    app = create_mcp_app()
    result = runner.invoke(app, ["status", "remote", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert '"logged_in": false' in result.output
    assert "OBSOLETE_TOKEN" not in result.output
    result = runner.invoke(app, ["logout", "remote", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "removed" in result.output


def test_unknown_server_and_hardened_mode_are_rejected(config_path):
    runner = CliRunner()
    result = runner.invoke(create_mcp_app(), ["login", "unknown", "--config", str(config_path)])
    assert result.exit_code == 1
    data = json.loads(config_path.read_text())
    data["tools"]["hardenedMode"] = True
    config_path.write_text(json.dumps(data))
    result = runner.invoke(create_mcp_app(), ["login", "remote", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "hardenedMode" in result.output
