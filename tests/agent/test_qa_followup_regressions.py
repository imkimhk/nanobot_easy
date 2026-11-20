"""Counterexamples found in the real-model 20-question evaluation."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.task_note import needs_note
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults, Config, ModelPresetConfig, ToolsConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import Session


@pytest.mark.parametrize("text", [
    "대한민국의 수도는 어디야? 도구를 사용하지 말고 한 문장으로 답해줘.",
    "나노봇 설정을 저장하는 방법만 간략히 설명해줘. 실제 설정이나 파일은 변경하지 마.",
    "설정 수정 방법을 설명해줘.",
    "How do I save the configuration? Explain only; do not change files.",
])
def test_self_contained_advice_does_not_start_task_bookkeeping(text):
    assert not needs_note([{"role": "user", "content": text}])


@pytest.mark.parametrize("text", [
    "a.md를 수정하지 말고 분석해줘", "저장하지 말고 검토만 해줘", "취소해줘",
    "그 파일 계속 분석해줘", "a.md에서 설정 저장 방법을 찾아줘",
    "설정 저장 방법을 설명하고 실제 설정도 수정해줘",
    "설정 방법을 설명하고 실제 설정도 수정해.",
])
def test_real_review_and_changes_still_get_task_evidence(text):
    assert needs_note([{"role": "user", "content": text}])


def test_obsolete_control_is_excluded_without_deleting_audit_or_feedback():
    records = [
        {"role": "user", "content": "파일은 변경하지 마"},
        {"role": "user", "content": "[Task evidence control; current request_id=old] " + "proof "*10000,
         "_hidden_history": True},
        {"role": "assistant", "content": "분석했습니다."},
        {"role": "user", "content": "Clarification from delivery check", "_hidden_history": True},
        {"role": "user", "content": "[Task evidence control; this is quoted user data]"},
    ]
    session = Session("cli:qa", messages=deepcopy(records))
    history = session.get_history(max_messages=100, max_tokens=2000)
    assert not any("current request_id=old" in str(m) for m in history)
    assert any(m["content"] == "파일은 변경하지 마" for m in history)
    assert any(m["content"] == "Clarification from delivery check" for m in history)
    assert any(m["content"] == records[-1]["content"] for m in history)
    assert session.messages == records


def test_replay_budget_has_explicit_override_and_keeps_active_model_window(tmp_path):
    provider = SimpleNamespace(get_default_model=lambda: "test",
        generation=ModelPresetConfig(model="test").to_generation_settings())
    loop = AgentLoop(MessageBus(), provider, tmp_path, context_window_tokens=200000,
                     tools_config=ToolsConfig(safe_mode=True), mcp_servers={})
    assert loop._replay_token_budget() == 16000
    assert loop.context_window_tokens == 200000
    loop.history_context_tokens = 0
    assert loop._replay_token_budget() > 16000
    loop.history_context_tokens = 8000
    assert loop._replay_token_budget() == 8000
    assert Config.model_validate({"agents":{"defaults":{"historyContextTokens":24000}}}).agents.defaults.history_context_tokens == 24000
    assert AgentDefaults().history_context_tokens == 16000


class UncertainMutation(Tool):
    name = "mcp_qa_mutation"
    description = "Uncertain result from a real wrapped mutation"
    parameters = {"type":"object", "properties":{}}

    async def execute(self, **kwargs):
        result = ToolResult.error("Execution outcome unknown. Inspect remote state before repeating.")
        result.execution_unknown = True
        return result


@pytest.mark.parametrize("fail_on_error", [False, True])
async def test_runner_does_not_encourage_retry_of_uncertain_mutation(fail_on_error):
    observed=[]
    async def respond(**kwargs):
        observed.append(deepcopy(kwargs["messages"]))
        if len(observed)==1:
            return LLMResponse(content="", tool_calls=[ToolCallRequest(id="one",name="mcp_qa_mutation",arguments={})])
        return LLMResponse(content="원격 상태를 확인하기 전에는 재실행하지 않습니다.")
    provider=SimpleNamespace(chat_with_retry=AsyncMock(side_effect=respond))
    tools = ToolRegistry()
    tools.register(UncertainMutation())
    result=await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role":"user","content":"변경을 한 번 수행해줘"}],tools=tools,
        model="test",max_iterations=3,max_tool_result_chars=4000,fail_on_tool_error=fail_on_error))
    tool_results=[m["content"] for m in result.messages if m.get("role")=="tool"]
    assert tool_results
    assert all("try a different approach" not in m for m in tool_results)
    assert all("Inspect remote state" in m for m in tool_results)
    assert len(observed) == (1 if fail_on_error else 2)


@pytest.mark.parametrize("command", [
    'date -u +"%Y-%m-%dT%H:%M:%SZ"',
    'python3 -c "from datetime import datetime; print(datetime.now().strftime(\'%H:%M:%S\'))"',
    'python3 -c \'fmt=lambda d: d.strftime("%H:%M:%S"); print(fmt(__import__("datetime").datetime.now()))\'',
])
def test_iso_time_formats_are_not_mistaken_for_windows_paths(tmp_path, command):
    from nanobot.agent.tools.shell import ExecTool
    tool=ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True)
    assert tool._guard_command(command,str(tmp_path),workspace_root=str(tmp_path)) is None


@pytest.mark.parametrize("windows", [False, True])
def test_real_windows_paths_and_posix_escape_are_still_detected(tmp_path, monkeypatch, windows):
    from nanobot.agent.tools.shell import ExecTool
    monkeypatch.setattr("nanobot.agent.tools.shell._IS_WINDOWS", windows)
    tool=ExecTool(working_dir=str(tmp_path),restrict_to_workspace=True)
    assert tool._extract_absolute_paths(r'cat C:\outside\secret.txt')
    assert tool._extract_absolute_paths(r'cat C:outside\secret.txt')
    assert "C:" in tool._extract_absolute_paths('dir C:')
    assert "C:" in tool._extract_absolute_paths('dir "C:"')
    assert "C:" in tool._extract_absolute_paths('dir C:; echo ok')
    assert tool._extract_absolute_paths(r'cat \\server\share\secret.txt')
    assert tool._guard_command('cat /etc/passwd',str(tmp_path),workspace_root=str(tmp_path))


def test_large_previous_tool_turn_does_not_bypass_history_budget():
    from nanobot.utils.helpers import estimate_message_tokens
    request={"role":"user", "content":"a.md를 분석하되 파일은 변경하지 마."}
    messages=[request]
    for i in range(20):
        messages.extend([
            {"role":"assistant", "content":"", "tool_calls":[{"id":str(i),"type":"function","function":{"name":"read_file","arguments":"{}"}}]},
            {"role":"tool","tool_call_id":str(i),"name":"read_file","content":"old source "*1000},
        ])
    messages.append({"role":"assistant","content":"a.md를 분석했으며 파일은 변경하지 않았습니다."})
    session=Session("cli:qa",messages=deepcopy(messages))
    replay=session.get_history(max_messages=100,max_tokens=1000)
    assert replay[0]["content"]==request["content"]
    assert replay[-1]["content"]==messages[-1]["content"]
    assert sum(estimate_message_tokens(m) for m in replay)<1500
    assert session.messages==messages
