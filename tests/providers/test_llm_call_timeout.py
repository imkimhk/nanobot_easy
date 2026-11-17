"""Per-call wall-clock limit for provider attempts (streaming and non-streaming)."""

from __future__ import annotations

import asyncio

import pytest

import nanobot.agent.runner as runner_module
import nanobot.providers.base as base_module
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import (
    DEFAULT_LLM_CALL_TIMEOUT_S,
    LLM_CALL_TIMEOUT_ENV,
    MAX_LLM_CALL_TIMEOUT_S,
    LLMProvider,
    LLMResponse,
    ToolCallRequest,
    resolve_llm_call_timeout_s,
)


class _LogRecorder:
    """Stands in for a module logger; independent of global loguru enable/disable state."""

    def __init__(self) -> None:
        self.records: list[str] = []

    def _record(self, message: str, *args, **kwargs) -> None:
        self.records.append(message.format(*args, **kwargs))

    info = warning = debug = error = exception = _record


class _HangingProvider(LLMProvider):
    """Scripted responses; the string "hang" blocks (non-stream) or trickles (stream)."""

    _CHAT_RETRY_DELAYS = ()

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.calls = 0

    async def chat(self, **kwargs) -> LLMResponse:
        self.calls += 1
        item = self.script.pop(0) if self.script else "hang"
        if item == "hang":
            await asyncio.sleep(30)
        return item

    async def chat_stream(self, **kwargs) -> LLMResponse:
        self.calls += 1
        on_delta = kwargs.get("on_content_delta")
        while True:  # keeps sending bytes, so an idle timeout would never fire
            if on_delta:
                await on_delta(".")
            await asyncio.sleep(0.01)

    def get_default_model(self) -> str:
        return "test-model"


def test_resolve_llm_call_timeout_defaults_and_bounds():
    assert resolve_llm_call_timeout_s(env_value="") == DEFAULT_LLM_CALL_TIMEOUT_S
    assert resolve_llm_call_timeout_s(env_value="nope") == DEFAULT_LLM_CALL_TIMEOUT_S
    assert resolve_llm_call_timeout_s(env_value="0") is None
    assert resolve_llm_call_timeout_s(env_value="-5") is None
    assert resolve_llm_call_timeout_s(env_value="120") == 120.0
    assert resolve_llm_call_timeout_s(env_value="99999") == MAX_LLM_CALL_TIMEOUT_S
    for invalid in ["nan", "inf", "-inf"]:
        assert resolve_llm_call_timeout_s(env_value=invalid) == DEFAULT_LLM_CALL_TIMEOUT_S


async def test_hung_non_streaming_call_ends_as_timeout(monkeypatch):
    monkeypatch.setenv(LLM_CALL_TIMEOUT_ENV, "0.05")
    provider = _HangingProvider(["hang"])

    response = await asyncio.wait_for(provider.chat_with_retry(messages=[{"role": "user", "content": "x"}]), 5)

    assert response.finish_reason == "error"
    assert response.error_kind == "timeout"
    assert provider.calls == 1


async def test_call_timeout_uses_existing_retry_policy(monkeypatch):
    monkeypatch.setenv(LLM_CALL_TIMEOUT_ENV, "0.05")
    provider = _HangingProvider(["hang", LLMResponse(content="ok")])
    provider._CHAT_RETRY_DELAYS = (0.01,)

    response = await asyncio.wait_for(provider.chat_with_retry(messages=[{"role": "user", "content": "x"}]), 5)

    assert response.content == "ok"
    assert provider.calls == 2


async def test_trickling_stream_is_bounded_by_call_timeout(monkeypatch):
    monkeypatch.setenv(LLM_CALL_TIMEOUT_ENV, "0.1")
    provider = _HangingProvider([])
    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    response = await asyncio.wait_for(provider.chat_stream_with_retry(
        messages=[{"role": "user", "content": "x"}], on_content_delta=on_delta,
    ), 5)

    assert response.finish_reason == "error"
    assert response.error_kind == "timeout"
    assert deltas


async def test_disabled_call_timeout_keeps_waiting(monkeypatch):
    monkeypatch.setenv(LLM_CALL_TIMEOUT_ENV, "0")
    provider = _HangingProvider(["hang"])
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider.chat_with_retry(messages=[{"role": "user", "content": "x"}]), 0.2)


class _OkTool(Tool):
    name = "check"
    description = "check"
    parameters = {"type": "object", "properties": {}, "required": []}
    read_only = True

    async def execute(self, **kwargs):
        return "done"


async def test_turn_hung_after_tool_result_ends_with_error_report(monkeypatch):
    monkeypatch.setenv(LLM_CALL_TIMEOUT_ENV, "0.05")
    provider = _HangingProvider([
        LLMResponse(content="", tool_calls=[ToolCallRequest(id="c1", name="check", arguments={})],
                    finish_reason="tool_calls"),
        "hang",
    ])
    tools = ToolRegistry()
    tools.register(_OkTool())
    recorder = _LogRecorder()
    monkeypatch.setattr(runner_module, "logger", recorder)
    monkeypatch.setattr(base_module, "logger", recorder)
    result = await asyncio.wait_for(AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "check it"}], tools=tools,
        model="test-model", max_iterations=4, max_tool_result_chars=10_000,
        session_key="telegram:1", llm_timeout_s=0.0,  # no outer runner wall clock
    )), 5)
    records = recorder.records

    assert result.stop_reason == "error"
    assert result.error and "per-call limit" in result.final_content
    assert any(m.get("role") == "tool" and m.get("content") == "done" for m in result.messages)
    assert any("LLM attempt end" in r and "error_kind=timeout" in r for r in records)
    assert any("LLM request end" in r and "iteration=1" in r and "error_kind=timeout" in r
               for r in records)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("shorter", ["request", "call"])
async def test_call_timeout_preserves_the_shorter_request_deadline(monkeypatch, stream, shorter):
    from nanobot.agent import task_budget

    call_limit, request_limit = (5, 0.03) if shorter == "request" else (0.03, 5)
    monkeypatch.setenv(LLM_CALL_TIMEOUT_ENV, str(call_limit))
    budget = task_budget.TaskBudget("deadline-check", max_sends=3, deadline_s=request_limit)
    monkeypatch.setattr(task_budget, "active_budget", lambda: budget)
    provider = _HangingProvider(["hang"])
    method = provider.chat_stream_with_retry if stream else provider.chat_with_retry
    response = await asyncio.wait_for(method(messages=[{"role": "user", "content": "x"}]), 1)
    assert response.error_kind == ("budget_exhausted" if shorter == "request" else "timeout")
    assert budget.sends == 1
    assert budget.unknown_usage == 1  # An interrupted call has unknown usage, not zero.
    assert budget.stop_reason == ("budget_deadline" if shorter == "request" else None)
