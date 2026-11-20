"""Real AnyIO cancellation scopes must never belong to the agent/request task."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import mcp
import mcp.client.stdio
import pytest

from nanobot.agent.tools import mcp as integration
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import MCPServerConfig


@pytest.fixture
def transport(monkeypatch):
    entered, exited, failures = {}, {}, {}

    @asynccontextmanager
    async def stdio(params):
        name = params.command
        entered[name] = asyncio.current_task()
        failures[name] = anyio.Event()

        async def receive():
            await failures[name].wait()
            raise OSError("receive connection lost")

        try:
            async with anyio.create_task_group() as group:
                group.start_soon(receive)
                try:
                    yield name, object()
                finally:
                    group.cancel_scope.cancel()
        finally:
            exited[name] = asyncio.current_task()

    class Session:
        def __init__(self, read, write):
            self.name = read

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            pass

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    monkeypatch.setattr(mcp, "ClientSession", Session)
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", stdio)
    return entered, exited, failures


async def test_two_servers_close_from_request_task_in_either_order(transport):
    entered, exited, _ = transport
    stacks = await integration.connect_mcp_servers(
        {n: MCPServerConfig(command=n, enabled_tools=[]) for n in ("first", "second")},
        ToolRegistry(),
    )
    assert set(stacks) == {"first", "second"}
    try:
        await asyncio.create_task(stacks["first"].aclose())
        await asyncio.create_task(stacks["second"].aclose())
        assert entered == exited
        assert len(set(entered.values())) == 2
        assert asyncio.current_task() not in entered.values()
    finally:
        for stack in stacks.values():
            await stack.aclose()


async def test_receive_failure_does_not_cancel_caller_or_other_server(transport):
    entered, exited, failures = transport
    stacks = await integration.connect_mcp_servers(
        {n: MCPServerConfig(command=n, enabled_tools=[]) for n in ("bad", "good")},
        ToolRegistry(),
    )
    try:
        failures["bad"].set()
        async with asyncio.timeout(2):
            while "bad" not in exited:
                await asyncio.sleep(0.001)
        assert not asyncio.current_task().cancelling()
        assert "good" not in exited
        await asyncio.create_task(stacks["bad"].aclose())
        await asyncio.create_task(stacks["good"].aclose())
        assert entered == exited
    finally:
        for stack in stacks.values():
            await stack.aclose()


async def test_cancel_initialization_unwinds_owner_and_preserves_stop(monkeypatch):
    entered, exited = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def stdio(params):
        async with anyio.create_task_group():
            try:
                entered.set()
                await anyio.sleep_forever()
                yield  # pragma: no cover
            finally:
                exited.set()

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", stdio)
    task = asyncio.create_task(integration.connect_mcp_servers(
        {"pending": MCPServerConfig(command="pending")}, ToolRegistry()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(exited.wait(), 2)


async def test_cancel_later_server_also_closes_earlier_owner(transport, monkeypatch):
    entered, exited, _ = transport
    original = mcp.client.stdio.stdio_client
    pending = asyncio.Event()

    @asynccontextmanager
    async def stdio(params):
        if params.command == "pending":
            pending.set()
            await anyio.sleep_forever()
        async with original(params) as streams:
            yield streams

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", stdio)
    task = asyncio.create_task(integration.connect_mcp_servers(
        {n: MCPServerConfig(command=n, enabled_tools=[]) for n in ("first", "pending")},
        ToolRegistry()))
    await asyncio.wait_for(pending.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert entered == exited


async def test_real_sdk_stdio_call_and_close_from_different_task(tmp_path):
    import sys

    script = tmp_path / "server.py"
    script.write_text('''from mcp.server.fastmcp import FastMCP
server = FastMCP("isolation-test")
@server.tool()
def ping() -> str:
    return "pong"
server.run(transport="stdio")
''')
    registry = ToolRegistry()
    stacks = await asyncio.wait_for(integration.connect_mcp_servers(
        {"local": MCPServerConfig(command=sys.executable, args=[str(script)],
                                  enabled_tools=["ping"])}, registry), 15)
    assert "local" in stacks
    try:
        result = await asyncio.create_task(registry.get("mcp_local_ping").execute())
        assert "pong" in result
    finally:
        await asyncio.wait_for(asyncio.create_task(stacks["local"].aclose()), 5)


async def test_real_sdk_server_death_reconnects_without_replaying_mutation(tmp_path):
    import sys

    script, effects = tmp_path / "server.py", tmp_path / "effects.txt"
    script.write_text('''import os, sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP
server = FastMCP("death-test")
@server.tool()
def crash() -> str:
    with Path(sys.argv[1]).open("a") as handle:
        handle.write("effect\\n")
    os._exit(3)
@server.tool()
def ping() -> str:
    return "pong"
server.run(transport="stdio")
''')

    class State:
        pass

    state = State()
    state._mcp_servers = {"local": MCPServerConfig(command=sys.executable,
        args=[str(script), str(effects)], enabled_tools=["crash", "ping"], tool_timeout=5)}
    registry = ToolRegistry()
    state._mcp_stacks = await integration.connect_mcp_servers(state._mcp_servers, registry)
    integration._attach_reconnect_handlers(state, registry, state._mcp_stacks)
    try:
        result = await asyncio.wait_for(registry.get("mcp_local_crash").execute(), 10)
        assert result.is_error
        assert effects.read_text() == "effect\n"
        assert "pong" in await registry.get("mcp_local_ping").execute()
        assert not asyncio.current_task().cancelling()
    finally:
        for stack in state._mcp_stacks.values():
            await asyncio.wait_for(stack.aclose(), 5)
