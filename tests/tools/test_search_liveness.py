"""Exercise responsiveness, cooperative stop and scoped paths under a slow scan."""
import asyncio
import threading

import pytest

from nanobot.agent.tools.search import FindFilesTool, GrepTool
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    default_workspace_scope,
    reset_workspace_scope,
)


@pytest.mark.parametrize("kind", [GrepTool, FindFilesTool])
async def test_blocking_filesystem_does_not_block_other_coroutines(tmp_path, monkeypatch, kind):
    """Use the unchanged filesystem boundary, so the old implementation reproduces the stall."""
    (tmp_path / "a.txt").write_text("needle")
    tool = kind(workspace=tmp_path, allowed_dir=tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = tool._resolve

    def slow_resolve(path):
        entered.set()
        assert release.wait(2)
        return original(path)

    monkeypatch.setattr(tool, "_resolve", slow_resolve)
    timer = threading.Timer(1, release.set)
    timer.start()
    task = asyncio.create_task(tool.execute(**({"pattern": "needle"} if kind is GrepTool else {})))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(0.001)
            # This line must run while filesystem I/O is still waiting.
            assert not release.is_set(), "Filesystem search blocked the event loop"
            release.set()
            assert "a.txt" in await task
    finally:
        release.set()
        timer.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", [GrepTool, FindFilesTool])
async def test_slow_search_keeps_socket_responsive_and_stops_worker(tmp_path, monkeypatch, kind):
    (tmp_path / "a.txt").write_text("needle")
    tool = kind(workspace=tmp_path, allowed_dir=tmp_path)
    entered, exited = threading.Event(), threading.Event()
    iterator = "_iter_files" if kind is GrepTool else "_iter_paths"
    original = getattr(tool, iterator)

    def slow(root, **kwargs):
        stop = kwargs["stop"]
        entered.set()
        try:
            while not stop.wait(0.01):
                pass
            yield from original(root, **kwargs)
        finally:
            exited.set()

    # Preserve the positional stop argument used by grep's traversal.
    def grep_slow(root, stop):
        yield from slow(root, stop=stop)

    monkeypatch.setattr(tool, iterator, grep_slow if kind is GrepTool else slow)

    async def health(reader, writer):
        await reader.read(1)
        writer.write(b"ok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(health, "127.0.0.1", 0)
    task = asyncio.create_task(tool.execute(**({"pattern": "needle"} if kind is GrepTool else {})))
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                if task.done():
                    pytest.fail(f"Search ended before entering worker: {task.result()}")
                await asyncio.sleep(0.001)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1]), 1)
        writer.write(b"?")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(2), 1) == b"ok"
        writer.close()
        await writer.wait_closed()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert await asyncio.to_thread(exited.wait, 1), "Cancelled scan kept running"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("kind", [GrepTool, FindFilesTool])
async def test_worker_preserves_concurrent_workspace_context(tmp_path, kind):
    roots = [tmp_path / n for n in ("first", "second")]
    for root in roots:
        root.mkdir()
        (root / f"{root.name}.txt").write_text("needle")
    tool = kind(workspace=tmp_path, allowed_dir=tmp_path)

    async def search(root):
        token = bind_workspace_scope(default_workspace_scope(root, True))
        try:
            return await tool.execute(**({"pattern": "needle"} if kind is GrepTool else {}))
        finally:
            reset_workspace_scope(token)

    results = await asyncio.gather(*(search(root) for root in roots))
    assert "first.txt" in results[0] and "second.txt" not in results[0]
    assert "second.txt" in results[1] and "first.txt" not in results[1]
