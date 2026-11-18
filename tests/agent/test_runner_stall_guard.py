"""Announcement-only finals after a failed tool call, and repeated no-tool finals."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import nanobot.agent.runner as runner_module
from nanobot.agent.context import ContextBuilder
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import timeout_error_text
from nanobot.config.schema import AgentDefaults
from nanobot.providers.base import LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars
ANNOUNCE = (
    "Node는 맞춰 둔 상태입니다. 이어서 OpenClaw를 최신 2026.11.2로 올리고, 끝나면 "
    "게이트웨이 실행까지 확인하겠습니다. 다른 게이트웨이는 건드리지 않습니다."
)
FACT = (
    "오로라의 현재 배포 확인 코드는 A7Q-314입니다. 이전 코드 OLD-19는 폐기되었으며 "
    "지금 사용하는 확인 코드는 A7Q-314 하나입니다."
)


class _FakeTool(Tool):
    def __init__(self, name: str, *, read_only: bool, result):
        self._name = name
        self._read_only = read_only
        self._result = result
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return self._read_only

    async def execute(self, **kwargs):
        self.calls += 1
        return self._result


class _LogRecorder:
    """Stands in for a module logger; independent of global loguru enable/disable state."""

    def __init__(self) -> None:
        self.records: list[str] = []

    def _record(self, message: str, *args, **kwargs) -> None:
        self.records.append(message.format(*args, **kwargs))

    info = warning = debug = error = exception = _record


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _call(name: str, call_id: str = "call_1") -> LLMResponse:
    return LLMResponse(
        content="", tool_calls=[ToolCallRequest(id=call_id, name=name, arguments={})],
        finish_reason="tool_calls",
    )


def _spec(tools: ToolRegistry, **kwargs) -> AgentRunSpec:
    values = dict(
        initial_messages=[{"role": "user", "content": "OpenClaw 업데이트하고 게이트웨이 실행해줘"}],
        tools=tools, model="test-model", max_iterations=6,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS, session_key="telegram:1",
    )
    values.update(kwargs)
    return AgentRunSpec(**values)


def _provider(*responses: LLMResponse) -> MagicMock:
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=list(responses))
    return provider


def _timeout_tool() -> _FakeTool:
    return _FakeTool("run_update", read_only=False, result=ToolResult.error(timeout_error_text(420)))


async def test_announcement_after_tool_timeout_is_resumed_once_then_tool_call_runs():
    failing = _timeout_tool()
    check = _FakeTool("check_version", read_only=True, result="openclaw 2026.11.2")
    provider = _provider(
        _call("run_update"),
        LLMResponse(content=ANNOUNCE, tool_calls=[]),
        _call("check_version", "call_2"),
        LLMResponse(content="업데이트는 이미 완료되어 2026.11.2입니다.", tool_calls=[]),
    )

    result = await AgentRunner(provider).run(_spec(_registry(failing, check)))

    assert provider.chat_with_retry.await_count == 4
    assert check.calls == 1
    assert result.stop_reason == "completed"
    assert result.final_content == "업데이트는 이미 완료되어 2026.11.2입니다."
    index = next(i for i, m in enumerate(result.messages)
                 if "[Runtime check]" in str(m.get("content")))
    feedback, announcement = result.messages[index], result.messages[index - 1]
    assert feedback["role"] == "user" and feedback.get("_hidden_history") is True
    assert "run_update: error" in feedback["content"]
    assert announcement["role"] == "assistant" and announcement["content"] == ANNOUNCE
    assert announcement.get("_hidden_history") is True
    assert result.messages[index + 1]["tool_calls"][0]["function"]["name"] == "check_version"


async def test_same_announcement_after_resume_stops_with_blocked_report():
    provider = _provider(
        _call("run_update"),
        LLMResponse(content=ANNOUNCE, tool_calls=[]),
        LLMResponse(content=ANNOUNCE + " ", tool_calls=[]),
    )

    result = await AgentRunner(provider).run(_spec(_registry(_timeout_tool())))

    assert provider.chat_with_retry.await_count == 3
    assert result.stop_reason == "incomplete"
    assert ANNOUNCE not in result.final_content
    assert "마지막으로 확인된 상태" in result.final_content
    assert "run_update: 실패" in result.final_content
    assert "timed out after 420 seconds" in result.final_content
    assert result.messages[-1]["role"] == "assistant"
    assert result.messages[-1]["content"] == result.final_content


async def test_different_report_after_resume_is_delivered():
    report = "업데이트 명령이 420초 제한으로 종료되어 결과를 확인하지 못했습니다. 버전 확인이 필요합니다."
    provider = _provider(
        _call("run_update"),
        LLMResponse(content=ANNOUNCE, tool_calls=[]),
        LLMResponse(content=report, tool_calls=[]),
    )

    result = await AgentRunner(provider).run(_spec(_registry(_timeout_tool())))

    assert provider.chat_with_retry.await_count == 3
    assert result.stop_reason == "completed"
    assert result.final_content == report


async def test_successful_turn_has_no_extra_model_call():
    ok = _FakeTool("run_update", read_only=False, result="updated\nExit code: 0")
    provider = _provider(_call("run_update"), LLMResponse(content=ANNOUNCE, tool_calls=[]))

    result = await AgentRunner(provider).run(_spec(_registry(ok)))

    assert provider.chat_with_retry.await_count == 2
    assert result.stop_reason == "completed"
    assert result.final_content == ANNOUNCE


async def test_read_only_tool_error_is_not_resumed():
    missing = _FakeTool("read_thing", read_only=True, result=ToolResult.error("Error: file not found"))
    provider = _provider(
        _call("read_thing"), LLMResponse(content="파일이 없어 확인할 수 없습니다.", tool_calls=[]),
    )

    result = await AgentRunner(provider).run(_spec(_registry(missing)))

    assert provider.chat_with_retry.await_count == 2
    assert result.final_content == "파일이 없어 확인할 수 없습니다."


async def test_call_refused_before_execution_is_not_resumed():
    class _NeedsArg(_FakeTool):
        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    writer = _NeedsArg("write_thing", read_only=False, result="written")
    provider = _provider(
        _call("write_thing"), LLMResponse(content="경로가 없어 쓰지 못했습니다.", tool_calls=[]),
    )

    result = await AgentRunner(provider).run(_spec(_registry(writer)))

    assert writer.calls == 0
    assert provider.chat_with_retry.await_count == 2
    assert result.final_content == "경로가 없어 쓰지 못했습니다."
    assert result.tool_events[0]["status"] == "error"
    assert type(result.tool_events[0]) is not dict  # marker subclass, still compares as a dict
    assert result.tool_events[0] == dict(result.tool_events[0])


async def test_resume_respects_max_iterations():
    provider = _provider(_call("run_update"), LLMResponse(content=ANNOUNCE, tool_calls=[]))

    result = await AgentRunner(provider).run(_spec(_registry(_timeout_tool()), max_iterations=2))

    assert provider.chat_with_retry.await_count == 2
    assert result.final_content == ANNOUNCE


async def test_stall_guard_can_be_disabled():
    provider = _provider(_call("run_update"), LLMResponse(content=ANNOUNCE, tool_calls=[]))

    result = await AgentRunner(provider).run(_spec(_registry(_timeout_tool()), stall_guard=False))

    assert provider.chat_with_retry.await_count == 2
    assert result.final_content == ANNOUNCE


async def test_repeat_of_previous_session_final_is_not_resent():
    provider = _provider(LLMResponse(content=ANNOUNCE, tool_calls=[]))
    history = [
        {"role": "user", "content": "OpenClaw 업데이트하고 게이트웨이 실행해줘"},
        {"role": "assistant", "content": ANNOUNCE},
        {"role": "user", "content": "진행해"},
    ]

    result = await AgentRunner(provider).run(_spec(_registry(), initial_messages=history))

    assert provider.chat_with_retry.await_count == 1
    assert result.stop_reason == "incomplete"
    assert ANNOUNCE not in result.final_content
    assert "실행된 도구가 없습니다" in result.final_content


async def test_same_status_after_fresh_successful_check_is_delivered():
    check = _FakeTool("check_status", read_only=True, result="A7Q-314 is still active")
    provider = _provider(_call("check_status"), LLMResponse(content=FACT, tool_calls=[]))
    history = [{"role": "assistant", "content": FACT},
               {"role": "user", "content": "현재 상태를 다시 확인해줘"}]
    result = await AgentRunner(provider).run(_spec(_registry(check), initial_messages=history))
    assert result.stop_reason == "completed"
    assert result.final_content == FACT
    assert check.calls == 1


@pytest.mark.parametrize("user_request", [
    "그 코드를 다시 알려줘.",
    "이전 답변을 그대로 다시 보여주세요.",
    "한 번 더 설명해줘!",
    "Please repeat your previous answer.",
    "다시 말해줘.",
    "Tell me that code again, please.",
    "그 코드를 다시 알려줘.\n\n" + ContextBuilder._RUNTIME_CONTEXT_TAG
    + "\nCurrent Time: 2026-11-17\n" + ContextBuilder._RUNTIME_CONTEXT_END,
    [{"type": "text", "text": "그 코드를 다시 알려줘."},
     {"type": "text", "text": ContextBuilder._RECENT_MEMORY_TAG + "\n이전 작업 진행해"}],
])
async def test_explicit_answer_replay_is_delivered_without_extra_call(user_request):
    provider = _provider(LLMResponse(content=FACT, tool_calls=[]))
    history = [
        {"role": "user", "content": "오로라의 현재 배포 확인 코드가 뭐야?"},
        {"role": "assistant", "content": FACT},
        {"role": "user", "content": user_request},
        {"role": "user", "content": "진행해", "_hidden_history": True},
    ]

    result = await AgentRunner(provider).run(_spec(_registry(), initial_messages=history))

    assert provider.chat_with_retry.await_count == 1
    assert result.stop_reason == "completed"
    assert result.final_content == FACT
    assert not result.tool_events


@pytest.mark.parametrize("user_request", [
    "다시 실행하고 결과를 알려줘.",
    "그 코드를 다시 알려주고 게이트웨이를 실행해줘.",
    "이전 답변을 그대로 다시 보여주고 수정해줘.",
    "그 코드를 다시 알려줘. 그리고 sensitive.txt에 저장해줘.",
    "Repeat the update and tell me the result again.",
    "Repeat that.",
    "다시 해줘.",
    "Please repeat your previous answer and execute the next step.",
    "진행해\n\n" + ContextBuilder._RECENT_MEMORY_TAG + "\n그 코드를 다시 알려줘.",
])
async def test_execution_or_background_replay_words_do_not_bypass_stall_guard(user_request):
    provider = _provider(LLMResponse(content=ANNOUNCE, tool_calls=[]))
    history = [
        {"role": "user", "content": "OpenClaw 업데이트하고 게이트웨이 실행해줘"},
        {"role": "assistant", "content": ANNOUNCE},
        {"role": "user", "content": user_request},
    ]

    result = await AgentRunner(provider).run(_spec(_registry(), initial_messages=history))

    assert provider.chat_with_retry.await_count == 1
    assert result.stop_reason == "incomplete"
    assert ANNOUNCE not in result.final_content


async def test_answer_replay_does_not_disable_failed_tool_or_same_turn_repeat_guard():
    provider = _provider(
        _call("run_update"),
        LLMResponse(content=FACT, tool_calls=[]),
        LLMResponse(content=FACT, tool_calls=[]),
    )
    history = [
        {"role": "assistant", "content": FACT},
        {"role": "user", "content": "그 코드를 다시 알려줘."},
    ]
    failing = _timeout_tool()

    result = await AgentRunner(provider).run(_spec(_registry(failing), initial_messages=history))

    assert provider.chat_with_retry.await_count == 3
    assert failing.calls == 1
    assert result.stop_reason == "incomplete"
    assert "run_update: 실패" in result.final_content
    assert any("[Runtime check]" in str(m.get("content")) for m in result.messages)


async def test_short_or_different_finals_are_not_treated_as_repeats():
    history = [
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"},
        {"role": "user", "content": "ping"},
    ]
    provider = _provider(LLMResponse(content="pong", tool_calls=[]))
    result = await AgentRunner(provider).run(_spec(_registry(), initial_messages=history))
    assert result.final_content == "pong"
    assert result.stop_reason == "completed"

    history[1]["content"] = ANNOUNCE
    provider = _provider(LLMResponse(content="OpenClaw 2026.11.2 업데이트와 게이트웨이 실행 확인을 마쳤습니다.",
                                     tool_calls=[]))
    result = await AgentRunner(provider).run(_spec(_registry(), initial_messages=history))
    assert result.stop_reason == "completed"


async def test_repeat_after_injected_followup_stops_instead_of_resending():
    pending = [[{"role": "user", "content": "왜 멈췄어? 계속해"}]]

    async def inject(**_kwargs):
        return pending.pop() if pending else []

    provider = _provider(
        LLMResponse(content=ANNOUNCE, tool_calls=[]),
        LLMResponse(content=ANNOUNCE, tool_calls=[]),
    )

    result = await AgentRunner(provider).run(_spec(_registry(), injection_callback=inject))

    assert provider.chat_with_retry.await_count == 2
    assert result.had_injections is True
    assert result.stop_reason == "incomplete"
    assert ANNOUNCE not in result.final_content


async def test_llm_requests_are_logged_without_prompt_content(monkeypatch):
    recorder = _LogRecorder()
    monkeypatch.setattr(runner_module, "logger", recorder)
    provider = _provider(LLMResponse(content="secret-answer-text", tool_calls=[]))
    await AgentRunner(provider).run(_spec(
        _registry(), initial_messages=[{"role": "user", "content": "secret-prompt-text"}],
    ))
    records = recorder.records

    start = [r for r in records if "LLM request start" in r]
    end = [r for r in records if "LLM request end" in r]
    assert len(start) == 1 and len(end) == 1
    assert "session=telegram:1" in start[0] and "iteration=0" in start[0]
    assert "model=test-model" in end[0] and "duration=" in end[0]
    assert "finish_reason=stop" in end[0] and "error_kind=-" in end[0]
    assert not any("secret-prompt-text" in r or "secret-answer-text" in r for r in records)
