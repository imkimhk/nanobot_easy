"""Exercise deletion with late writers using temporary sessions and fake models."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.websocket import WebSocketConfig
from nanobot.config.schema import ToolsConfig
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope
from nanobot.memory.task_context import TaskContextStore
from nanobot.providers.base import LLMResponse
from nanobot.session.lifecycle import SessionDeletedError
from nanobot.session.manager import SessionManager
from nanobot.session.webui_turns import maybe_generate_webui_title
from nanobot.webui.gateway_services import build_gateway_services


def gateway_for(workspace, manager, bus, **kwargs):
    return build_gateway_services(
        config=WebSocketConfig(token="synthetic-token", workspace_controls_enabled=False),
        bus=bus, session_manager=manager, static_dist_path=None, workspace_path=workspace,
        default_restrict_to_workspace=True, runtime_model_name=None,
        runtime_surface="browser", runtime_capabilities_overrides=None, **kwargs,
    )


def delete_request(gateway, key, retain=False):
    return SimpleNamespace(
        path=f"/api/sessions/{key}/delete?delete_memory={'false' if retain else 'true'}"
             "&delete_automations=true",
        headers={"Authorization": "Bearer " + gateway.tokens.issue_token(300, api_token=True)},
    )


def test_old_session_objects_cannot_resurrect_or_overwrite_new_generation(tmp_path):
    manager = SessionManager(tmp_path)
    key = "websocket:deleted"
    old = manager.get_or_create(key)
    old.add_message("user", "deleted private text")
    manager.save(old)
    detached = deepcopy(old)
    manager.invalidate(key)
    reloaded = manager.get_or_create(key)
    assert manager.delete_session(key)
    for stale in (old, detached, reloaded):
        assert manager.save(stale, fsync=True) is False
    assert not manager._get_session_path(key).exists()
    assert SessionManager(tmp_path).read_session_file(key) is None

    fresh = manager.get_or_create(key)
    fresh.add_message("user", "new conversation")
    assert manager.save(fresh)
    for stale in (old, detached, reloaded):
        assert manager.save(stale) is False
    assert manager.read_session_file(key)["messages"] == fresh.messages


async def test_child_memory_writer_cannot_recreate_deleted_events_or_tasks(tmp_path):
    manager = SessionManager(tmp_path)
    key = "websocket:deleted"
    old = manager.get_or_create(key)
    manager.save(old)
    store = ConversationEventStore(tmp_path)
    tasks = TaskContextStore(store)
    scope = MemoryScope.from_runtime(owner_id="owner", workspace_id=str(tmp_path), agent_id=None)
    original = store.append_event(
        scope=scope, conversation_id=key, session_id=key, actor="user",
        event_type="USER_MESSAGE", content="deleted private text",
    )
    task_state = tasks.create(scope, key, original, "private task", ["done"])
    release = asyncio.Event()

    async def late_writer():
        await release.wait()
        tasks.save(scope, task_state, expected_version=task_state["version"])

    with manager.session_scope(old):
        child = asyncio.create_task(late_writer())
    store.purge_session(key)
    manager.delete_session(key)
    fresh = manager.get_or_create(key)
    manager.save(fresh)
    release.set()
    with pytest.raises(SessionDeletedError):
        await child
    assert tasks.list(scope, key) == []
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


@pytest.mark.parametrize("retain", [False, True])
@pytest.mark.parametrize("dispatch", [False, True])
async def test_delete_during_real_agent_turn_does_not_resurrect_session(
    tmp_path, monkeypatch, retain, dispatch,
):
    monkeypatch.setattr("nanobot.config.loader._current_config_path", tmp_path / "config.json")
    entered, release = asyncio.Event(), asyncio.Event()
    provider = MagicMock()
    provider.get_default_model.return_value = "synthetic-model"

    async def answer(**kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Even a provider that returns late after cancellation must not restore data.
            await release.wait()
        return LLMResponse(content="synthetic completed reply")

    provider.chat_with_retry = AsyncMock(side_effect=answer)
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus, provider=provider, workspace=tmp_path,
        tools_config=ToolsConfig(hardened_mode=True, restrict_to_workspace=True),
    )
    key = "websocket:delete-race"
    async def send(content):
        if dispatch:
            return await loop._dispatch(InboundMessage(
                channel="websocket", sender_id="owner", chat_id="delete-race", content=content,
            ))
        return await loop.process_direct(
            content, session_key=key, channel="websocket", chat_id="delete-race",
        )

    task = asyncio.create_task(send("synthetic user input"))
    queued = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        queued = asyncio.create_task(send("queued old input"))
        await asyncio.sleep(0)  # Let this request reach the held per-session lock.
        gateway = gateway_for(tmp_path, loop.sessions, bus)
        response = gateway.http._handle_session_delete(delete_request(gateway, key, retain), key)
        assert response.status_code == 200
        assert json.loads(response.body)["deleted"]
        release.set()
        results = await asyncio.wait_for(asyncio.gather(task, queued, return_exceptions=True), 10)
        assert all(result is None or isinstance(result, asyncio.CancelledError) for result in results)
        assert not loop.sessions._get_session_path(key).exists()
        with loop.conversation_memory.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM events WHERE session_id=?", (key,)).fetchone()[0]
        assert count == (1 if retain else 0)
        # A deliberate new message can reuse the key; it cannot recover the old history.
        fresh_result = await loop.process_direct(
            "new user input", session_key=key, channel="websocket", chat_id="delete-race",
        )
        assert fresh_result.content == "synthetic completed reply"
        saved = loop.sessions.read_session_file(key)
        assert not any(m.get("content") == "synthetic user input" for m in saved["messages"])
    finally:
        release.set()
        if not task.done():
            task.cancel()
        if queued is not None:
            queued.cancel()
        await asyncio.gather(*[t for t in (task, queued) if t is not None], return_exceptions=True)
        for background in list(loop._background_tasks):
            background.cancel()
        await asyncio.gather(*list(loop._background_tasks), return_exceptions=True)


@pytest.mark.parametrize("recreate", [False, True])
async def test_late_title_cannot_restore_deleted_chat_or_overwrite_fresh_chat(tmp_path, recreate):
    manager = SessionManager(tmp_path)
    key = "websocket:title-race"
    session = manager.get_or_create(key)
    session.metadata["webui"] = True
    session.add_message("user", "old title input")
    session.add_message("assistant", "old answer")
    manager.save(session)
    entered, release = asyncio.Event(), asyncio.Event()

    async def title(*args, **kwargs):
        entered.set()
        await release.wait()
        return LLMResponse(content="old generated title")

    provider = SimpleNamespace(chat_with_retry=title)
    task = asyncio.create_task(maybe_generate_webui_title(
        sessions=manager, session_key=key, provider=provider, model="synthetic",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        manager.delete_session(key)
        if recreate:
            fresh = manager.get_or_create(key)
            fresh.add_message("user", "new title input")
            fresh.metadata["title"] = "new title"
            manager.save(fresh)
        release.set()
        assert await asyncio.wait_for(task, 2) is False
        data = manager.read_session_file(key)
        if recreate:
            assert data["metadata"]["title"] == "new title"
            assert data["messages"][0]["content"] == "new title input"
        else:
            assert data is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_memory_purge_failure_does_not_remove_automations_or_session(tmp_path, monkeypatch):
    manager = SessionManager(tmp_path)
    key = "websocket:purge-failure"
    manager.save(manager.get_or_create(key))
    ConversationEventStore(tmp_path)
    gateway = gateway_for(tmp_path, manager, MessageBus())
    cron = MagicMock()
    gateway.http.cron_service = cron
    monkeypatch.setattr(
        "nanobot.webui.ws_http.session_automation_jobs",
        lambda *args, **kwargs: [SimpleNamespace(id="kept-job")],
    )

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic purge failure")

    monkeypatch.setattr(ConversationEventStore, "purge_session", fail)
    with pytest.raises(RuntimeError, match="synthetic purge failure"):
        gateway.http._handle_session_delete(delete_request(gateway, key), key)
    cron.remove_job.assert_not_called()
    assert manager._get_session_path(key).exists()
