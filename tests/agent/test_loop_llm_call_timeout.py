"""A model call that hangs after a tool result ends the turn and clears pending state."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLM_CALL_TIMEOUT_ENV, LLMProvider, LLMResponse, ToolCallRequest


class _ToolThenHangProvider(LLMProvider):
    _CHAT_RETRY_DELAYS = ()

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(self, **kwargs) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
                finish_reason="tool_calls",
            )
        await asyncio.sleep(30)
        return LLMResponse(content="late")

    def get_default_model(self) -> str:
        return "test-model"


async def test_hung_model_call_after_tool_result_finishes_turn(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(LLM_CALL_TIMEOUT_ENV, "0.05")
    provider = _ToolThenHangProvider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]

    result = await asyncio.wait_for(loop._process_message(
        InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="폴더 목록 보여줘"),
    ), 10)

    assert provider.calls == 2
    assert result is not None and "per-call limit" in result.content
    session = loop.sessions.get_or_create("telegram:c1")
    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata
    assert any(m.get("role") == "tool" for m in session.messages)
