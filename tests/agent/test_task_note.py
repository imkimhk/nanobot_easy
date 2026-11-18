"""v4.2 counterexamples: scripted models, real local tools, isolated targets."""
import asyncio
import json
import re
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.task_note import (
    Target,
    TaskNote,
    TaskUpdate,
    UpdateTaskTool,
    active_task_note,
    bounded_operation,
    needs_note,
)
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config, StepwiseConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import SessionManager


def target(path="a.md", **kwargs):
    return {"path": path, "selection_reason": "current conversation selects this file", **kwargs}


def update(note, *, targets=None, **kwargs):
    return TaskUpdate.model_validate({
        "request_id": note.request_id, "intent": "new", "goal": note.request,
        "targets": targets if targets is not None else [target()], "source": "files",
        "analysis_complete": True, "status": "completed", "explanation": "analysis performed",
        "final_answer": "검토 결과입니다.",
        "result_refs": [e["id"] for e in note.events if e["mutating"] and e["status"] == "ok"],
        **kwargs,
    })


class Script:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.calls = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        step = next(self.steps)
        return step(kwargs) if callable(step) else step


def control(**changes):
    answer = changes.pop("answer", "")
    def response(kwargs):
        guidance = next(m["content"] for m in reversed(kwargs["messages"])
                        if isinstance(m.get("content"), str) and "current request_id=" in m["content"])
        request_id = re.search(r"current request_id=([a-f0-9]+)", guidance)[1]
        args = {"request_id": request_id, "intent": "new", "goal": "review requested file",
                "targets": [target()], "source": "files", "analysis_complete": True,
                "status": "completed", "explanation": "analysis performed",
                "final_answer": answer or "검토 결과입니다.", **changes}
        return LLMResponse(content=answer, tool_calls=[ToolCallRequest(
            id="control-" + str(len(kwargs["messages"])), name="update_task", arguments=args)])
    return response


def call(name, **args):
    return LLMResponse(content="", tool_calls=[ToolCallRequest(id=name, name=name, arguments=args)])


def final(text="검토 결과: 문제를 확인했습니다."):
    return LLMResponse(content=text, tool_calls=[])


async def run(tmp_path, steps, request="a.md 전체 파일을 검토해 줘", **kwargs):
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    tools.register(kwargs.pop("write_tool", None) or WriteFileTool(workspace=tmp_path))
    provider = Script(steps)
    metadata = kwargs.pop("task_note_metadata", {})
    spec = AgentRunSpec(initial_messages=[{"role": "user", "content": request}],
                        tools=tools, model="test", max_iterations=kwargs.pop("max_iterations", 12),
                        max_tool_result_chars=8000,
                        workspace=tmp_path, task_note_enabled=True,
                        task_note_metadata=metadata, **kwargs)
    result = await AgentRunner(provider).run(spec)
    return result, provider, metadata


async def read(note, tmp_path, name="a.md", **kwargs):
    tool = ReadFileTool(workspace=tmp_path)
    token = active_task_note.set(note)
    try:
        result = await tool.execute(path=name, force=True, **kwargs)
    finally:
        active_task_note.reset(token)
    note.observe("read_file", {"path": name}, result, tool, "ok")
    return result


def test_t01_old_text_never_confirms_new_intent(tmp_path):
    note = TaskNote([{"role": "assistant", "content": "요청 해석: 계속"},
                     {"role": "user", "content": "저장하지 말고 검토만 해."}], tmp_path, {})
    assert note.defects() == ["intent_unconfirmed"]
    assert note.mutation_error("write_file", {"path": "a.md"})
    stale = update(note)
    note.revise([{"role": "user", "content": "변경하지 마"}])
    assert note.accept(stale).is_error


async def test_t01_pending_followup_prevents_prepared_write(tmp_path):
    injections = AsyncMock(side_effect=[
        [{"role": "user", "content": "저장하지 말고 검토만 해."}], [], [], [], [], [],
    ])
    result, _, metadata = await run(tmp_path, [
        call("write_file", path="a.md", content="unapproved"),
        control(source="none", targets=[], status="clarification", analysis_complete=False,
                explanation="검토할 보고서의 대상을 확인해야 합니다."),
        final("저장하지 않았습니다. 검토할 보고서를 지정해 주세요."),
    ], request="보고서를 수정하고 저장해 줘", injection_callback=injections)
    assert not (tmp_path / "a.md").exists()
    assert result.stop_reason == "incomplete"
    assert result.tool_events[0]["status"] == "superseded"
    assert metadata["task_note"]["intent"] == "new"


@pytest.mark.parametrize("status", ["completed", "cancelled"])
@pytest.mark.parametrize("iterations", [3, 6])
async def test_withdrawal_before_write_is_cancelled_without_goal_resume(tmp_path, status, iterations):
    (tmp_path / "a.md").write_text("original", encoding="utf-8")
    prepared = False
    injected = False

    def prepare_write(kwargs):
        nonlocal prepared
        prepared = True
        return call("write_file", **write_args("changed"))

    async def cancel(**kwargs):
        nonlocal injected
        if prepared and not injected:
            injected = True
            return [{"role": "user", "content": "취소해줘. a.md를 수정하거나 저장하지 마."}]
        return []

    result, provider, metadata = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        prepare_write,
        control(intent="withdraw", targets=[target(action="write")], source="none",
                status=status, answer="취소했습니다. 저장 작업은 실행되지 않았습니다."),
    ], request="a.md에 changed를 저장해줘.", injection_callback=cancel,
        max_iterations=iterations, goal_active_predicate=lambda: True)
    assert result.stop_reason == "cancelled"
    assert "취소했습니다" in result.final_content
    assert "execution_required" not in result.final_content
    assert "미완료" not in result.final_content
    assert (tmp_path / "a.md").read_text() == "original"
    assert len(provider.calls) == 3
    assert next(e for e in result.tool_events if e["name"] == "write_file")["status"] == "superseded"
    assert metadata["task_note"]["status"] == "cancelled"


async def test_withdrawal_keeps_prior_mutation_audit_without_claiming_rollback(tmp_path):
    injected = False

    async def cancel_after_save(**kwargs):
        nonlocal injected
        if (tmp_path / "a.md").exists() and not injected:
            injected = True
            return [{"role": "user", "content": "저장 작업을 취소해줘. 추가 변경하지 마."}]
        return []

    result, provider, metadata = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        call("write_file", **write_args("already saved")),
        control(intent="withdraw", targets=[target(action="write")], source="none",
                answer="취소했습니다."),
    ], request="a.md를 저장해줘.", injection_callback=cancel_after_save)
    assert result.stop_reason == "cancelled"
    assert (tmp_path / "a.md").read_text() == "already saved"
    assert result.tools_used.count("write_file") == 1
    prior = metadata["task_note"]["superseded_mutations"]
    assert len(prior) == 1 and prior[0]["status"] == "ok"
    assert prior[0]["request_id"] != metadata["task_note"]["request_id"]
    assert "superseded_mutations" in str(provider.calls[-1]["messages"])
    assert "원복" in result.final_content


async def test_withdrawal_unknown_save_is_preserved_and_can_be_verified_read_only(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 저장"}], tmp_path, {})
    note.accept(update(note, targets=[target(action="write")], status="active"))
    tool = WriteFileTool(workspace=tmp_path)
    args = write_args("saved content")
    await tool.execute(**args)
    note.observe("write_file", args, "timeout after saving", tool, "error", "uncertain-write")
    note.revise([{"role": "user", "content": "취소해줘. 추가 저장하지 마."}])
    note.accept(update(note, intent="withdraw", source="none", targets=[target(action="write")],
                       status="cancelled", analysis_complete=True, final_answer="취소했습니다."))
    assert note.defects() == []
    assert note.mutation_error("write_file", args)
    assert "결과 미확인" in note.final_report()
    await read(note, tmp_path)
    assert note.superseded_mutations[0]["verification"] == "read_after_unknown_write"
    note.accept(update(note, intent="withdraw", source="none", targets=[target(action="write")],
                       status="cancelled", analysis_complete=True, final_answer="취소했습니다."))
    assert "실행 성공" in note.final_report()
    assert (tmp_path / "a.md").read_text() == "saved content"


def test_withdrawal_requires_report_and_cancelled_status_requires_withdrawal(tmp_path):
    assert needs_note([{"role": "user", "content": "취소해줘"}])
    note = TaskNote([{"role": "user", "content": "취소해줘"}], tmp_path, {})
    note.accept(update(note, intent="withdraw", targets=[], source="none", status="cancelled",
                       analysis_complete=True, final_answer=""))
    assert note.defects() == ["answer_required"]
    tool = UpdateTaskTool(note)
    with pytest.raises(ValueError, match="requires intent=withdraw"):
        update(note, intent="new", status="cancelled")
    assert "cancelled" in tool.parameters["properties"]["status"]["enum"]


async def test_cancellation_during_write_records_unknown_effect_without_undo(tmp_path):
    started = asyncio.Event()

    class PartialWrite(WriteFileTool):
        supports_cancellation = True

        async def execute(self, **kwargs):
            self._resolve_write(kwargs["path"]).write_text("partial effect", encoding="utf-8")
            started.set()
            await asyncio.Event().wait()

    metadata = {}
    task = asyncio.create_task(run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        call("write_file", **write_args("complete effect")),
    ], request="a.md 저장해줘", write_tool=PartialWrite(workspace=tmp_path),
        task_note_metadata=metadata))
    try:
        await asyncio.wait_for(started.wait(), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    note = metadata["task_note"]
    assert note["status"] == "cancelled"
    assert note["events"][0]["status"] == "unknown"
    assert note["events"][0]["target"] == str((tmp_path / "a.md").resolve())
    assert note["attempts"][-1]["cleanup"] == "settled"
    assert (tmp_path / "a.md").read_text() == "partial effect"


async def test_t02_all_targets_required_and_wrong_file_rejected(tmp_path):
    (tmp_path / "a.md").write_text("A", encoding="utf-8")
    (tmp_path / "b.md").write_text("B", encoding="utf-8")
    note = TaskNote([{"role": "user", "content": "그 설계서 두 개를 비교 검토해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[target(), target("b.md")]))
    await read(note, tmp_path)
    note.accept(update(note, targets=[target(), target("b.md")]))
    assert note.defects() == ["coverage_unconfirmed:b.md"]
    await read(note, tmp_path, "b.md")
    note.accept(update(note, targets=[target(), target("b.md")]))
    assert note.defects() == []


async def test_t03_ambiguous_then_user_selects(tmp_path):
    (tmp_path / "b.md").write_text("selected design", encoding="utf-8")
    result, _, _ = await run(tmp_path, [
        control(targets=[], status="clarification", analysis_complete=False,
                explanation="동명 설계서 두 개 중 어떤 버전인지 필요합니다."),
        final("설계서의 1판과 2판 중 어느 것을 검토할까요?"),
    ], request="설계서 파일을 검토해 줘")
    assert result.stop_reason == "incomplete"
    result, _, _ = await run(tmp_path, [control(targets=[target("b.md")]),
                                        call("read_file", path="b.md"),
                                        control(targets=[target("b.md")], answer="검토 결과입니다.")],
                            request="2판 b.md 파일을 검토해 줘")
    assert result.stop_reason == "completed"


async def test_t04_unknown_contract_and_whole_word_do_not_prove_coverage(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 전체를 검토해 줘"}], tmp_path, {})
    note.accept(update(note))
    note.observe("read_file", {"path": "a.md"}, "1| 전체 시스템의 첫 페이지", object(), "ok")
    note.accept(update(note))
    assert note.defects() == ["coverage_unconfirmed:a.md"]


async def test_t05_full_read_and_requested_range(tmp_path):
    (tmp_path / "a.md").write_text("\n".join(f"line {n}" for n in range(1, 201)), encoding="utf-8")
    result, provider, _ = await run(tmp_path, [
        control(), call("read_file", path="a.md", limit=300), control(answer="검토 결과입니다."),
    ])
    assert result.stop_reason == "completed"
    assert len(provider.calls) == 3
    note = TaskNote([{"role": "user", "content": "a.md 2절만 검토해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[target(unit="lines", start=50, end=80)]))
    await read(note, tmp_path, offset=1, limit=10)
    assert note.defects()
    await read(note, tmp_path, offset=50, limit=31)
    note.accept(update(note, targets=[target(unit="lines", start=50, end=80)]))
    assert note.defects() == []


async def test_t05_multiple_partial_reads_cover_whole(tmp_path):
    (tmp_path / "a.md").write_text("a\nb\nc\nd", encoding="utf-8")
    note = TaskNote([{"role": "user", "content": "a.md 전체 검토"}], tmp_path, {})
    note.accept(update(note))
    await read(note, tmp_path, offset=1, limit=2)
    assert note.defects()
    await read(note, tmp_path, offset=3, limit=2)
    note.accept(update(note))
    assert note.defects() == []


async def test_t06_cancellation_settles_child_and_never_changes_new_note(tmp_path):
    metadata = {}
    note = TaskNote([{"role": "user", "content": "파일 수정"}], tmp_path, metadata)
    started, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def delayed():
        started.set()
        await release.wait()
        effects.append("late change")

    parent = asyncio.create_task(bounded_operation(note, "delayed", delayed))
    await started.wait()
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    next_note = TaskNote([{"role": "user", "content": "다른 파일 검토"}], tmp_path, metadata)
    release.set()
    await asyncio.sleep(0)
    assert effects == []
    assert note.attempts[0]["cleanup"] == "settled"
    assert metadata["task_note"]["run_id"] == next_note.run_id


async def test_t07_delivery_failure_then_redelivery_never_repeats_save(tmp_path):
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("telegram:test")
    session.metadata["task_note"] = {"run_id": "run", "status": "completed"}
    config = Config.model_validate({"agents": {"defaults": {"stepwise": {"lightweight": True}}},
                                    "channels": {"websocket": {"enabled": False}, "sendMaxRetries": 1}})
    manager = ChannelManager(config, MessageBus(), session_manager=sessions)
    channel = type("Channel", (), {"send": AsyncMock(side_effect=RuntimeError("delivery failed"))})()
    msg = OutboundMessage(channel="telegram", chat_id="test", content="saved",
                          metadata={"_task_delivery": {"session_key": session.key, "run_id": "run"}})
    await manager._send_with_retry(channel, msg)
    assert session.metadata["task_note"]["status"] == "completed"
    assert session.metadata["task_note"]["delivery"] == "failed"
    channel.send = AsyncMock()
    await manager._send_with_retry(channel, msg)
    assert session.metadata["task_note"]["delivery"] == "sent"
    assert sessions.get_or_create(session.key).metadata["task_deliveries"]["run"]["attempts"] == 1


def test_t08_denied_and_unknown_writes_differ(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 저장해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[target(action="write")]))
    tool = type("Writer", (), {"read_only": False})()
    note.observe("write_file", {"path": "a.md"}, "permission denied", tool, "error")
    note.accept(update(note, targets=[target(action="write")]))
    assert note.defects() == ["blocked:a.md"]
    note.events.clear()
    note.observe("write_file", {"path": "a.md"}, "timeout", tool, "error")
    note.accept(update(note, targets=[target(action="write")]))
    assert note.defects() == ["record_check:a.md"]
    assert note.mutation_error("write_file", {"path": "a.md"})


async def test_t09_multiple_defects_and_no_repeat_of_successful_write(tmp_path):
    (tmp_path / "a.md").write_text("source", encoding="utf-8")
    note = TaskNote([{"role": "user", "content": "a.md 검토하고 b.md 저장해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[target(), target("b.md", action="write")], analysis_complete=False))
    assert set(note.defects()) == {"coverage_unconfirmed:a.md", "execution_required:b.md", "analysis_incomplete"}
    note.observe("write_file", {"path": "b.md"}, "saved", type("Writer", (), {"read_only": False})(), "ok")
    assert note.mutation_error("write_file", {"path": "b.md"})
    await read(note, tmp_path)
    note.accept(update(note, targets=[target(), target("b.md", action="write")]))
    assert note.defects() == []


async def test_t10_pasted_body_and_valid_cached_read(tmp_path):
    result, _, _ = await run(tmp_path, [control(source="provided", targets=[]), final()],
                            request="이 본문에서 문제점을 검토해 줘.\n```supplied text``` ")
    assert result.stop_reason == "completed"
    (tmp_path / "a.md").write_text("source", encoding="utf-8")
    metadata = {}
    note = TaskNote([{"role": "user", "content": "a.md 검토"}], tmp_path, metadata)
    await read(note, tmp_path)
    cached = TaskNote([{"role": "user", "content": "a.md 다시 검토"}], tmp_path, metadata)
    cached.accept(update(cached))
    assert cached.defects() == []
    (tmp_path / "a.md").write_text("changed", encoding="utf-8")
    assert cached.defects()


async def test_t11_budget_exhaustion_cannot_report_success(tmp_path):
    tools = ToolRegistry()
    provider = Script([final("전체 파일 검토 완료"), final("검토 완료")])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "a.md 전체 검토"}],
        tools=tools, model="test", max_iterations=1, max_tool_result_chars=8000,
        task_note_enabled=True, task_note_metadata={}, workspace=tmp_path))
    assert result.stop_reason == "incomplete"
    assert "완료하지 못했습니다" in result.final_content


def test_t12_current_intent_outranks_memory_and_old_approval(tmp_path):
    messages = [{"role": "user", "content": "a.md 저장해 줘"},
                {"role": "assistant", "content": "요청 해석: 계속"},
                {"role": "user", "content": "저장하지 말고 검토만 해."}]
    note = TaskNote(messages, tmp_path, {})
    note.accept(update(note, intent="change", source="none", targets=[]))
    assert note.mutation_error("write_file", {"path": "a.md"})


async def test_simple_and_other_agents_have_no_control_tool_or_extra_call(tmp_path):
    result, provider, metadata = await run(tmp_path, [final("2")], request="1+1은?")
    assert len(provider.calls) == 1
    assert not any(d["function"]["name"] == "update_task" for d in provider.calls[0]["tools"])
    assert metadata == {}
    assert StepwiseConfig().lightweight is False
    assert result.final_content == "2"


def test_reference_code_block_does_not_replace_requested_file(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 파일 전체 검토. 참고 ```example```"}], tmp_path, {})
    note.accept(update(note, source="provided", targets=[]))
    assert "target_unresolved" in note.defects()


class Calculation(Tool):
    name = "calculate"
    description = "test calculation"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    async def execute(self, **kwargs):
        return ToolResult("4")


async def test_t11_free_tool_switch_with_real_file(tmp_path):
    (tmp_path / "a.md").write_text("2+2", encoding="utf-8")
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    tools.register(Calculation())
    result = await AgentRunner(Script([control(), call("read_file", path="a.md"),
                                       call("calculate"), control(answer="계산 결과는 4입니다.")])).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "a.md 파일 분석"}], tools=tools,
        model="test", max_iterations=8, max_tool_result_chars=8000, workspace=tmp_path,
        task_note_enabled=True, task_note_metadata={}))
    assert result.stop_reason == "completed"
    assert result.tools_used == ["update_task", "read_file", "calculate", "update_task"]


def write_args(content="saved content"):
    return {"path": "a.md", "content": content, "intent_summary": "save requested report",
            "target": "a.md", "scope": "once", "reversible": True}


async def test_t07_real_save_and_failed_send_preserve_execution(tmp_path):
    result, provider, metadata = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        call("write_file", **write_args()),
        control(targets=[target(action="write")], result_refs=["write_file"], answer="a.md에 저장했습니다."),
    ], request="a.md에 보고서를 저장해 줘")
    assert result.stop_reason == "completed"
    assert (tmp_path / "a.md").read_text() == "saved content"
    stamp = (tmp_path / "a.md").stat().st_mtime_ns
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("telegram:save-test")
    session.metadata.update(metadata)
    config = Config.model_validate({"agents": {"defaults": {"stepwise": {"lightweight": True}}},
                                    "channels": {"websocket": {"enabled": False}, "sendMaxRetries": 1}})
    manager = ChannelManager(config, MessageBus(), session_manager=sessions)
    channel = type("Channel", (), {"send": AsyncMock(side_effect=RuntimeError("failed"))})()
    msg = OutboundMessage(channel="telegram", chat_id="save-test", content=result.final_content,
                          metadata={"_task_delivery": {"session_key": session.key,
                                                       "run_id": metadata["task_note"]["run_id"]}})
    await manager._send_with_retry(channel, msg)
    assert session.metadata["task_note"]["status"] == "completed"
    assert session.metadata["task_note"]["delivery"] == "failed"
    channel.send = AsyncMock()
    await manager._send_with_retry(channel, msg)
    assert session.metadata["task_note"]["delivery"] == "sent"
    assert (tmp_path / "a.md").stat().st_mtime_ns == stamp
    assert len(provider.calls) == 3
    assert result.tools_used.count("write_file") == 1


class SaveThenTimeout(WriteFileTool):
    async def execute(self, **kwargs):
        await super().execute(**kwargs)
        raise TimeoutError("write response timed out after saving")


async def test_t08_real_saved_timeout_reconciled_by_read_without_second_save(tmp_path):
    result, _, metadata = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        call("write_file", **write_args()), call("read_file", path="a.md"),
        control(targets=[target(action="write")], result_refs=["write_file"], answer="저장 결과를 확인했습니다."),
    ], request="a.md에 보고서를 저장해 줘", write_tool=SaveThenTimeout(workspace=tmp_path))
    assert result.stop_reason == "completed"
    events = metadata["task_note"]["events"]
    writes = [e for e in events if e["name"] == "write_file"]
    assert len(writes) == 1
    assert writes[0]["verification"] == "read_after_unknown_write"


async def test_t09_missing_result_reference_is_linked_without_another_model_call(tmp_path):
    result, provider, metadata = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        call("write_file", **write_args()),
        control(targets=[target(action="write")], answer="저장했습니다."),
    ], request="a.md 저장해 줘")
    assert result.stop_reason == "completed"
    assert result.tools_used.count("write_file") == 1
    assert len(provider.calls) == 3
    assert not any("Completion defects" in str(m.get("content")) for m in result.messages)
    assert metadata["task_note"]["update"]["result_refs"] == ["write_file"]
    assert metadata["task_note"]["runtime_linked_result_refs"] == ["write_file"]


@pytest.mark.parametrize("changes", [
    {"analysis_complete": False}, {"status": "active"}, {"status": "partial"},
    {"final_answer": ""}, {"result_refs": ["invented-id"]},
])
def test_runtime_linking_does_not_repair_other_completion_defects(tmp_path, changes):
    note = TaskNote([{"role": "user", "content": "a.md 저장해 줘"}], tmp_path, {})
    note.observe("write_file", write_args(), "saved", WriteFileTool(workspace=tmp_path),
                 "ok", "actual-write")
    note.accept(update(note, targets=[target(action="write")],
                       **{"result_refs": [], **changes}))
    assert note.runtime_linked_result_refs == []
    assert any("missing_result_reference" in d for d in note.defects())


@pytest.mark.parametrize("status", ["unknown", "denied", "error"])
def test_runtime_linking_never_uses_an_old_success_after_a_new_failure(tmp_path, status):
    note = TaskNote([{"role": "user", "content": "a.md 저장해 줘"}], tmp_path, {})
    writer = WriteFileTool(workspace=tmp_path)
    note.observe("write_file", write_args(), "saved", writer, "ok", "earlier-write")
    note.observe("write_file", write_args(), status, writer, status, "latest-write")
    note.accept(update(note, targets=[target(action="write")], result_refs=[]))
    assert note.runtime_linked_result_refs == []
    assert note.update.result_refs == []
    assert note.defects()


def test_runtime_links_all_current_targets_and_resets_on_new_request(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md와 b.md 저장해 줘"}], tmp_path, {})
    writer = WriteFileTool(workspace=tmp_path)
    for name, identity in [("a.md", "actual-a"), ("b.md", "actual-b")]:
        note.observe("write_file", {**write_args(), "path": name}, "saved", writer, "ok", identity)
    note.accept(update(note, targets=[target("a.md", action="write"), target("b.md", action="write")],
                       result_refs=["actual-a"]))
    assert note.defects() == []
    assert note.update.result_refs == ["actual-a", "actual-b"]
    assert note.runtime_linked_result_refs == ["actual-b"]
    note.revise([{"role": "user", "content": "a.md 저장해 줘"}])
    note.accept(update(note, targets=[target(action="write")], result_refs=[]))
    assert note.runtime_linked_result_refs == []
    assert note.defects() == ["execution_required:a.md"]


async def test_runtime_linking_requires_read_coverage_and_verified_unknown_write(tmp_path):
    result, provider, metadata = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        call("write_file", **write_args()), call("read_file", path="a.md"),
        control(targets=[target(action="write"), target()], result_refs=[], answer="저장을 확인했습니다."),
    ], request="a.md를 저장하고 다시 읽어 확인해줘.", write_tool=SaveThenTimeout(workspace=tmp_path))
    note = metadata["task_note"]
    assert result.stop_reason == "completed" and len(provider.calls) == 4
    assert sum(e["name"] == "write_file" for e in note["events"]) == 1
    assert result.tools_used.count("read_file") == 1
    assert note["runtime_linked_result_refs"] == ["write_file"]
    assert next(e for e in note["events"] if e["name"] == "write_file")["verification"] == "read_after_unknown_write"


def test_missing_read_coverage_prevents_runtime_linking(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md를 저장하고 b.md도 읽어줘"}], tmp_path, {})
    note.observe("write_file", write_args(), "saved", WriteFileTool(workspace=tmp_path),
                 "ok", "actual-write")
    note.accept(update(note, targets=[target(action="write"), target("b.md")], result_refs=[]))
    assert note.runtime_linked_result_refs == []
    assert "coverage_unconfirmed:b.md" in note.defects()


@pytest.mark.parametrize("timeout", [False, True])
async def test_write_reference_recovery_uses_only_model_visible_results(tmp_path, timeout):
    call_id = "call_opaque_transport_id|fc_opaque_item_id"

    def recover(kwargs):
        response = next(m for m in reversed(kwargs["messages"])
                        if m.get("role") == "tool" and "write_results" in m.get("content", ""))
        writes = json.loads(response["content"])["write_results"]
        assert len(writes) == 1
        assert writes[0]["status"] == "ok"
        if timeout:
            assert writes[0]["verification"] == "read_after_unknown_write"
        return control(targets=[target(action="write")],
                       result_refs=[writes[0]["id"]], answer="a.md 저장을 확인했습니다.")(kwargs)

    result, _, metadata = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        LLMResponse(content="", tool_calls=[ToolCallRequest(id=call_id, name="write_file",
                                               arguments=write_args())]),
        call("read_file", path="a.md"),
        control(targets=[target(action="write")], result_refs=["functions.write_file"],
                answer="저장했습니다."),
        recover,
    ], request="a.md 저장해 줘", write_tool=(SaveThenTimeout if timeout else WriteFileTool)(
        workspace=tmp_path))
    assert result.stop_reason == "completed"
    assert len([e for e in metadata["task_note"]["events"] if e["name"] == "write_file"]) == 1
    assert metadata["task_note"]["update"]["result_refs"] == [call_id]
    assert any("missing_result_reference" in str(m.get("content")) for m in result.messages)


def test_write_results_exclude_old_requests_and_keep_latest_status(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 저장"}], tmp_path, {})
    tool = WriteFileTool(workspace=tmp_path)
    note.observe("write_file", write_args(), "saved", tool, "ok", "old-success")
    note.observe("write_file", write_args(), "timeout", tool, "error", "latest-unknown")
    writes = note.write_results()
    assert [e["id"] for e in writes] == ["latest-unknown"]
    assert writes[0]["status"] == "unknown"
    assert "write_results" in note.guidance()["content"]
    note.revise([{"role": "user", "content": "저장 취소"}])
    assert note.write_results() == []


async def test_provided_write_payload_requires_execution_not_file_acquisition(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md에 saved content 저장"}], tmp_path, {})
    note.accept(update(note, source="provided", targets=[target(action="write")]))
    assert note.defects() == ["execution_required:a.md"]
    note.observe("write_file", write_args(), "saved", WriteFileTool(workspace=tmp_path),
                 "ok", "write-id")
    note.accept(update(note, source="provided", targets=[target(action="write")], result_refs=[]))
    assert note.defects() == []
    assert note.update.result_refs == ["write-id"]
    note.accept(update(note, source="provided", targets=[target(action="write")]))
    assert note.defects() == []
    note.accept(update(note, source="provided", targets=[target()]))
    assert "target_unresolved" in note.defects()
    assert "coverage_unconfirmed:a.md" in note.defects()
    note.accept(update(note, source="provided", targets=[]))
    assert "target_unresolved" in note.defects()
    (tmp_path / "a.md").write_text("saved content", encoding="utf-8")
    await read(note, tmp_path)
    note.accept(update(note, source="provided", targets=[target(action="write"), target()]))
    assert note.defects() == []


async def test_t06_noncooperative_late_result_is_audit_only(tmp_path):
    metadata = {}
    note = TaskNote([{"role": "user", "content": "파일 수정"}], tmp_path, metadata)
    started, release = asyncio.Event(), asyncio.Event()

    async def noncooperative():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return "external operation already completed"

    parent = asyncio.create_task(bounded_operation(note, "remote", noncooperative, grace=0.01))
    await started.wait()
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    next_note = TaskNote([{"role": "user", "content": "다른 파일 검토"}], tmp_path, metadata)
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert metadata["task_note"]["run_id"] == next_note.run_id
    audit = metadata["task_runs"][note.run_id]
    assert audit["status"] == "cancelled"
    assert audit["attempts"][0]["late_result"] == "returned_after_cancellation"
    assert next_note.events == []


async def test_search_worker_matches_real_search_and_retains_parameters(tmp_path):
    from nanobot.agent.tools.search import FindFilesTool, GrepTool
    from nanobot.agent.tools.search_worker import isolated_search

    (tmp_path / "a.md").write_text("alpha\nbeta", encoding="utf-8")
    (tmp_path / "b.py").write_text("alpha", encoding="utf-8")
    for tool, params in [
        (FindFilesTool(workspace=tmp_path), {"query": "a", "glob": "*.md", "head_limit": 1}),
        (GrepTool(workspace=tmp_path), {"pattern": "alpha", "glob": "*.md", "output_mode": "content"}),
    ]:
        expected = await tool.execute(**params)
        observed = await isolated_search(tool, params)
        assert observed == expected


async def test_model_context_offload_is_not_full_acquisition(tmp_path):
    (tmp_path / "a.md").write_text("x" * 2000, encoding="utf-8")
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    result = await AgentRunner(Script([
        control(), call("read_file", path="a.md"), control(answer="전체 검토 완료"),
        control(status="partial", explanation="한 줄이 문맥 한도를 초과해 전체 내용을 확인하지 못했습니다.",
                answer="자료 일부만 확인했습니다."),
    ])).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "a.md 전체 검토"}], tools=tools,
        model="test", max_iterations=8, max_tool_result_chars=500, workspace=tmp_path,
        task_note_enabled=True, task_note_metadata={}))
    assert result.stop_reason == "incomplete"


async def test_pdf_page_contract_and_partial_page_request(tmp_path):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    for n in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {n + 1}")
    doc.save(tmp_path / "a.pdf")
    doc.close()
    note = TaskNote([{"role": "user", "content": "a.pdf 2페이지만 검토"}], tmp_path, {})
    note.accept(update(note, targets=[target("a.pdf", unit="pages", start=2, end=2)]))
    await read(note, tmp_path, "a.pdf", pages="1")
    note.accept(update(note, targets=[target("a.pdf", unit="pages", start=2, end=2)]))
    assert note.defects()
    await read(note, tmp_path, "a.pdf", pages="2")
    note.accept(update(note, targets=[target("a.pdf", unit="pages", start=2, end=2)]))
    assert note.defects() == []


async def test_disabled_agent_does_not_gain_task_tools(tmp_path):
    tools = ToolRegistry()
    provider = Script([final("평소 답변")])
    metadata = {}
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "파일을 검토해 줘"}], tools=tools,
        model="test", max_iterations=3, max_tool_result_chars=8000, workspace=tmp_path,
        task_note_enabled=False, task_note_metadata=metadata))
    assert result.final_content == "평소 답변"
    assert metadata == {}
    assert provider.calls[0]["tools"] == []
    assert not tools.has("update_task")


def test_named_document_cannot_be_reclassified_as_no_source(tmp_path):
    note = TaskNote([{"role": "user", "content": "그 설계서 파일을 검토해 줘"}], tmp_path, {})
    note.accept(update(note, source="none", targets=[]))
    assert "target_unresolved" in note.defects()


async def test_unicode_filename_with_spaces_is_linked(tmp_path):
    name = "설계서 최종.md"
    (tmp_path / name).write_text("actual design", encoding="utf-8")
    note = TaskNote([{"role": "user", "content": f"'{name}'를 검토해 줘"}], tmp_path, {})
    await read(note, tmp_path, name)
    note.accept(update(note, targets=[target(str(tmp_path / name))]))
    assert note.defects() == []


class DeniedWrite(WriteFileTool):
    async def execute(self, **kwargs):
        return ToolResult.error("permission denied before saving")


async def test_t08_permission_denial_finishes_blocked_without_retry(tmp_path):
    result, _, metadata = await run(tmp_path, [
        control(targets=[target(action="write")], status="active", analysis_complete=False),
        call("write_file", **write_args()),
        control(targets=[target(action="write")], status="blocked", analysis_complete=False,
                explanation="권한이 거부되어 저장하지 못했습니다.", answer="저장 권한이 없습니다."),
    ], request="a.md 저장해 줘", write_tool=DeniedWrite(workspace=tmp_path))
    assert result.stop_reason == "incomplete"
    assert not (tmp_path / "a.md").exists()
    assert len([e for e in metadata["task_note"]["events"] if e["name"] == "write_file"]) == 1


async def test_search_child_is_killed_on_cancellation(tmp_path, monkeypatch):
    import sys

    from nanobot.agent.tools import search_worker
    from nanobot.agent.tools.search import FindFilesTool

    create = asyncio.create_subprocess_exec
    started = asyncio.Event()
    children = []

    async def delayed_worker(*args, **kwargs):
        process = await create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        children.append(process)
        started.set()
        return process

    monkeypatch.setattr(search_worker.asyncio, "create_subprocess_exec", delayed_worker)
    task = asyncio.create_task(search_worker.isolated_search(FindFilesTool(workspace=tmp_path), {}))
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert children[0].returncode is not None


async def test_candidate_completion_text_is_not_progress(tmp_path):
    from nanobot.agent.hook import AgentHookContext
    from nanobot.agent.progress_hook import AgentProgressHook

    callback = AsyncMock()
    hook = AgentProgressHook(on_progress=callback)
    note = TaskNote([{"role": "user", "content": "파일 검토"}], tmp_path, {})
    token = active_task_note.set(note)
    try:
        await hook.before_execute_tools(AgentHookContext(
            iteration=0, messages=[], response=LLMResponse(content="검토 완료했습니다."), tool_calls=[]))
    finally:
        active_task_note.reset(token)
    assert all("검토 완료" not in str(call) for call in callback.await_args_list)


async def test_t01_followup_between_read_and_write_in_same_response(tmp_path):
    (tmp_path / "a.md").write_text("original", encoding="utf-8")

    def prepared_actions(kwargs):
        response = control(targets=[target(), target(action="write")],
                           status="active", analysis_complete=False)(kwargs)
        response.tool_calls.extend([
            ToolCallRequest(id="read", name="read_file", arguments={"path": "a.md"}),
            ToolCallRequest(id="write", name="write_file", arguments=write_args("obsolete")),
        ])
        return response

    followups = AsyncMock(side_effect=[[], [], [], [
        {"role": "user", "content": "a.md 저장하지 말고 검토만 해."},
    ], *([[]] * 12)])
    result, _, _ = await run(tmp_path, [
        prepared_actions, control(intent="change", answer="변경 없이 원문을 검토했습니다."),
    ], request="a.md 파일을 검토하고 수정해 저장해 줘", injection_callback=followups,
                            concurrent_tools=True)
    assert result.stop_reason == "completed"
    assert (tmp_path / "a.md").read_text() == "original"
    assert next(e for e in result.tool_events if e["name"] == "write_file")["status"] == "superseded"


async def test_t12_retrieved_memory_and_skill_paths_are_not_new_instructions(tmp_path):
    from nanobot.agent.context import ContextBuilder

    request = ("1+1은?\n\n" + ContextBuilder._RECENT_MEMORY_TAG
               + "\n과거 요청: report.md를 저장해 줘.\n" + ContextBuilder._RECENT_MEMORY_END
               + "\n\n" + ContextBuilder._RUNTIME_CONTEXT_TAG
               + "\nSkill: /somewhere/SKILL.md; 검토 및 파일 수정\n" + ContextBuilder._RUNTIME_CONTEXT_END)
    result, provider, metadata = await run(tmp_path, [final("2")], request=request)
    assert result.final_content == "2"
    assert len(provider.calls) == 1
    assert metadata == {}


def test_old_string_evidence_is_not_promoted_or_reused(tmp_path):
    metadata = {"task_note": {"evidence": ["read_file:ok:a.md"]}}
    note = TaskNote([{"role": "user", "content": "a.md 검토"}], tmp_path, metadata)
    assert note.evidence == []
    assert note.defects() == ["intent_unconfirmed"]


def test_metadata_only_versions_cannot_merge_distinct_reads(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 전체 검토"}], tmp_path, {})
    note.accept(update(note))
    note.evidence = [{"target": str(tmp_path / "a.md"), "unit": "lines", "start": start,
                      "end": end, "total": 10, "extraction_ok": True,
                      "request_id": note.request_id, "version": {"size": 2_000_000, "mtime_ns": 1}}
                     for start, end in [(1, 5), (6, 10)]]
    assert note.defects() == ["coverage_unconfirmed:a.md"]


def test_distinct_authorized_edits_are_not_blocked_as_delivery_duplicates(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 수정해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[target(action="write")]))
    tool = type("Writer", (), {"read_only": False})()
    params = {"path": "a.md", "old_text": "old", "new_text": "new"}
    note.observe("edit_file", params, "saved", tool, "ok")
    assert note.mutation_error("edit_file", {**params, "path": "./a.md"})
    assert note.mutation_error("edit_file", {**params, "old_text": "another"}) is None


async def test_analysis_complete_without_public_answer_is_not_completion(tmp_path):
    (tmp_path / "a.md").write_text("The same start operation ran three times.", encoding="utf-8")
    note = TaskNote([{"role": "user", "content": "a.md 원인 분석"}], tmp_path, {})
    await read(note, tmp_path)
    note.accept(update(note, explanation="start was repeated three times", final_answer=""))
    assert "answer_required" in note.defects()


async def test_observed_closure_notice_recovers_report_without_rereading(tmp_path):
    (tmp_path / "a.md").write_text("06:52 start\n06:53 start\n06:54 cancelled", encoding="utf-8")
    report = "원인은 초기화 뒤 start를 반복 호출한 것입니다. 로그에 두 번의 시작과 마지막 취소가 기록돼 있습니다."
    result, provider, _ = await run(tmp_path, [
        call("read_file", path="a.md"),
        control(answer="필요한 구간은 모두 확인됐습니다. 원인 판정을 닫겠습니다.",
                explanation="Repeated start calls were found", final_answer=""),
        control(answer=report, final_answer=report),
    ])
    assert result.final_content == report
    assert result.tools_used.count("read_file") == 1
    assert len(provider.calls) == 3


async def test_structured_answer_is_delivered_instead_of_status_sentence(tmp_path):
    (tmp_path / "a.md").write_text("duplicate trigger", encoding="utf-8")
    report = "원인은 같은 트리거가 중복 실행된 것입니다. 자료에서 중복 호출을 확인했습니다."
    result, provider, _ = await run(tmp_path, [
        call("read_file", path="a.md"),
        control(answer="판정에 필요한 기록은 이미 확인됐습니다. 그 범위로 결론을 닫겠습니다.",
                final_answer=report),
    ])
    assert result.stop_reason == "completed"
    assert result.final_content == report
    assert len(provider.calls) == 2


async def test_continue_analysis_reuses_evidence_but_requires_actual_report(tmp_path):
    (tmp_path / "a.md").write_text("start repeated twice", encoding="utf-8")
    metadata = {}
    old_note = TaskNote([{"role": "user", "content": "a.md 원인을 파악해 줘"}], tmp_path, metadata)
    await read(old_note, tmp_path)
    report = "기록상 start를 두 번 호출해 반복됐습니다. 추가 전송 여부는 별도 확인이 필요합니다."
    result, provider, _ = await run(tmp_path, [
        control(intent="continue", answer="판정에 필요한 기록은 이미 확인됐습니다. 그 범위로 결론을 닫겠습니다.",
                final_answer=""),
        control(intent="continue", final_answer=report),
    ], request="판정을 계속해 줘", task_note_metadata=metadata)
    assert result.final_content == report
    assert result.tools_used == ["update_task", "update_task"]
    assert len(provider.calls) == 2


async def test_reporting_recovery_still_allowed_after_coverage_recovery(tmp_path):
    (tmp_path / "a.md").write_text("start repeated twice", encoding="utf-8")
    report = "원인은 같은 시작 작업의 중복 호출입니다. 로그에 호출 두 건이 있습니다."
    result, provider, _ = await run(tmp_path, [
        control(answer="검토 완료", final_answer=report),
        call("read_file", path="a.md"),
        control(answer="원인 판정을 닫겠습니다.", final_answer=""),
        control(final_answer=report),
    ])
    assert result.stop_reason == "completed"
    assert result.final_content == report
    assert result.tools_used.count("read_file") == 1
    assert len(provider.calls) == 4


async def test_reporting_recovery_cannot_repeat_mutation(tmp_path):
    report = "기존 자료만으로 원인은 아직 확정할 수 없습니다. 파일 변경은 수행하지 않았습니다."
    result, _, _ = await run(tmp_path, [
        control(source="provided", targets=[], answer="분석을 마무리하겠습니다.", final_answer=""),
        call("write_file", **write_args()),
        control(source="provided", targets=[], final_answer=report),
    ], request="제공된 본문을 분석해 줘.\n```sample text``` ")
    assert result.final_content == report
    assert not (tmp_path / "a.md").exists()
    assert next(e for e in result.tool_events if e["name"] == "write_file")["status"] == "blocked"


@pytest.mark.parametrize("answer", [
    "원인 판정을 닫겠습니다.",
    "필요한 구간은 모두 확인됐습니다. 원인 판정을 닫겠습니다.",
    "판정에 필요한 기록은 이미 확인됐습니다. 그 범위로 결론을 닫겠습니다.",
    "먼저 policy.py를 읽고 판정을 닫겠습니다.",
])
def test_closing_promises_are_not_a_public_report(tmp_path, answer):
    note = TaskNote([{"role": "user", "content": "이 본문 원인을 분석해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[], source="provided", final_answer=answer))
    assert note.defects() == ["answer_required"]


async def test_last_iteration_still_delivers_report_without_extra_model_call(tmp_path):
    (tmp_path / "a.md").write_text("two starts", encoding="utf-8")
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=tmp_path))
    report = "로그에서 시작 호출 두 건을 확인했습니다. 중복 시작이 원인입니다."

    def last_response(kwargs):
        response = control(final_answer=report)(kwargs)
        response.tool_calls.insert(0, ToolCallRequest(id="read", name="read_file", arguments={"path": "a.md"}))
        return response

    provider = Script([last_response])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "a.md 원인 분석"}], tools=tools,
        model="test", max_iterations=1, max_tool_result_chars=8000, workspace=tmp_path,
        task_note_enabled=True, task_note_metadata={}))
    assert result.stop_reason == "completed"
    assert result.final_content == report
    assert len(provider.calls) == 1


async def test_no_budget_cannot_complete_without_report(tmp_path):
    tools = ToolRegistry()
    provider = Script([control(source="provided", targets=[], final_answer="", answer="판정을 닫겠습니다.")])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "아래 본문의 원인을 분석해 줘\n```records```"}],
        tools=tools, model="test", max_iterations=1, max_tool_result_chars=8000, workspace=tmp_path,
        task_note_enabled=True, task_note_metadata={}))
    assert result.stop_reason == "incomplete"
    assert "answer_required" in result.final_content
    assert len(provider.calls) == 1


async def test_structured_report_still_passes_existing_public_content_filter(tmp_path):
    from nanobot.agent.progress_hook import AgentProgressHook

    result, _, metadata = await run(tmp_path, [
        control(source="provided", targets=[], final_answer="<think>private reasoning</think>원인은 중복 호출입니다."),
    ], request="제공된 본문을 분석해 줘\n```duplicate calls```", hook=AgentProgressHook())
    assert result.final_content == "원인은 중복 호출입니다."
    assert "private reasoning" not in metadata["task_note"]["update"]["final_answer"]


async def test_live_log_ranges_survive_append_without_claiming_new_tail(tmp_path):
    log = tmp_path / "live.log"
    log.write_text("one\ntwo\nthree\nfour\n")
    note = TaskNote([{"role": "user", "content": "최근 로그 원인 분석"}], tmp_path, {})
    await read(note, tmp_path, "live.log", offset=1, limit=2)
    with log.open("a") as stream:
        stream.write("five\n")
    await read(note, tmp_path, "live.log", offset=3, limit=2)
    note.accept(update(note, targets=[target("live.log", unit="lines", start=1, end=4)]))
    assert note.defects() == []
    assert not note.covered(Target.model_validate(target("live.log")))
    log.write_text("changed\ntwo\nthree\nfour\nfive\n")
    assert not note.covered(Target.model_validate(target("live.log", unit="lines", start=1, end=4)))


async def test_live_log_pagination_finishes_under_actual_model_limit(tmp_path):
    log = tmp_path / "live.log"
    log.write_text("\n".join(f"event {i}: " + "x" * 950 for i in range(1, 21)) + "\n")
    calls = 0

    def step(kwargs):
        nonlocal calls
        calls += 1
        observed = [m for m in kwargs["messages"] if m.get("role") == "tool"
                    and m.get("name") == "read_file"]
        if not observed:
            return call("read_file", path="live.log")
        output = observed[-1]["content"]
        assert len(output) <= 8000
        covered = [int(n) for n in re.findall(r"^(\d+)\|", output, re.M)]
        assert covered
        end = max(covered)
        if end >= 20:
            return control(targets=[target("live.log", unit="lines", start=1, end=20)],
                           final_answer="요청한 로그 1–20행의 검증을 마쳤습니다.")(kwargs)
        with log.open("a") as stream:
            stream.write("new runtime event\n")
        return call("read_file", path="live.log", offset=end + 1, limit=20 - end)

    result, provider, metadata = await run(tmp_path, [step] * 8, request="최근 로그를 분석해줘")
    assert result.stop_reason == "completed"
    assert len(provider.calls) <= 5
    assert all(e["extraction_ok"] for e in metadata["task_note"]["evidence"])


@pytest.mark.parametrize("status", ["partial", "blocked", "clarification"])
@pytest.mark.parametrize("max_iterations", [1, 8])
async def test_terminal_structured_report_is_delivered_without_extra_generation(
    tmp_path, status, max_iterations,
):
    report = "테스트 실행은 성공했습니다. 외부 서버 인증 확인은 남아 있습니다."
    result, provider, _ = await run(tmp_path, [
        control(source="none", targets=[], status=status, analysis_complete=False,
                final_answer=report, explanation="외부 로그인 필요"),
    ], request="동작을 분석해줘", max_iterations=max_iterations)
    assert result.stop_reason == "incomplete"
    assert report in result.final_content
    assert "외부 로그인 필요" in result.final_content
    assert len(provider.calls) == 1


@pytest.mark.parametrize("configured_timeout", [60, 300, 0])
async def test_exec_does_not_inherit_hidden_short_task_timeout(tmp_path, monkeypatch, configured_timeout):
    from nanobot.agent import task_note as module
    from nanobot.agent.tools.shell import ExecTool

    observed = []

    async def observe_timeout(note, name, operation, **kwargs):
        observed.append(kwargs.get("timeout", "default"))
        return await operation()

    tool = ExecTool(working_dir=str(tmp_path), timeout=configured_timeout)
    monkeypatch.setattr(tool, "execute", AsyncMock(return_value=ToolResult("Exit code: 0")))
    monkeypatch.setattr(module, "bounded_operation", observe_timeout)
    tools = ToolRegistry()
    tools.register(tool)
    provider = Script([
        control(source="none", targets=[], status="active", analysis_complete=False),
        call("exec", command="long-running fixture", timeout=300,
             intent_summary="run fixture", target="fixture", scope="once", reversible=True),
        control(source="none", targets=[], final_answer="명령 실행 결과를 확인했습니다."),
    ])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "명령 실행 결과를 분석해줘"}], tools=tools,
        model="test", max_iterations=4, max_tool_result_chars=8000, workspace=tmp_path,
        task_note_enabled=True, task_note_metadata={},
    ))
    assert result.stop_reason == "completed"
    assert observed[1] is None


def test_revisiting_an_earlier_file_content_is_not_a_duplicate_of_latest_write(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md를 수정해줘"}], tmp_path, {})
    note.accept(update(note, targets=[target(action="write")], status="active"))
    tool = WriteFileTool(workspace=tmp_path)
    first = {"path": "a.md", "content": "first"}
    second = {"path": "a.md", "content": "second"}
    note.observe("write_file", first, "saved", tool, "ok", "one")
    note.observe("write_file", second, "saved", tool, "ok", "two")
    assert note.mutation_error("write_file", first) is None
    assert note.mutation_error("write_file", second) is not None


@pytest.mark.parametrize("user_request", [
    "설정 저장 방법을 설명해줘", "수정해야 하나? 현재 문제를 분석해줘",
    "How do I edit this setting?", "Should I fix this configuration?",
    "설정 저장 방법을 설명해줘. 파일은 만들거나 변경하지 마.",
    "설정의 의미를 설명해줘. 파일을 만들지 마세요.",
    "Explain the setting without modifying files.",
])
def test_questions_about_editing_do_not_require_a_write(tmp_path, user_request):
    note = TaskNote([{"role": "user", "content": user_request}], tmp_path, {})
    note.accept(update(note, source="none", targets=[]))
    assert note.defects() == []


@pytest.mark.parametrize("user_request", [
    "결과를 저장해줘", "설정을 수정해 주세요", "잘못된 부분 고쳐줘",
    "Please fix the issue", "Can you edit the configuration?",
])
def test_actual_edit_requests_still_require_execution(tmp_path, user_request):
    note = TaskNote([{"role": "user", "content": user_request}], tmp_path, {})
    note.accept(update(note, source="none", targets=[]))
    assert "write_target_unresolved" in note.defects()


def test_prohibition_does_not_hide_the_actual_requested_read_target(tmp_path):
    note = TaskNote([{"role": "user", "content": "그 파일을 분석해줘. 파일은 만들거나 변경하지 마."}],
                    tmp_path, {})
    note.accept(update(note, source="none", targets=[]))
    assert "target_unresolved" in note.defects()


async def test_large_request_keeps_first_read_after_many_other_sources(tmp_path):
    metadata = {}
    note = TaskNote([{"role": "user", "content": "a.md를 분석해줘"}], tmp_path, metadata)
    (tmp_path / "a.md").write_text("requested source\n")
    await read(note, tmp_path)
    for i in range(80):
        name = f"diagnostic-{i}.txt"
        (tmp_path / name).write_text("supporting source\n")
        await read(note, tmp_path, name)
    note.accept(update(note))
    assert note.defects() == []
    assert len(metadata["task_note"]["evidence"]) == 81
    # Follow-up/restart can still use the selected original after large work.
    restored = TaskNote([{"role": "user", "content": "a.md 분석을 계속해줘"}], tmp_path, metadata)
    restored.accept(update(restored))
    assert restored.defects() == []
    # An unrelated new request releases old selected proofs into a bounded cache.
    fresh = TaskNote([{"role": "user", "content": "제공된 본문 분석"}], tmp_path, metadata)
    fresh.accept(update(fresh, source="provided", targets=[]))
    assert len(fresh.evidence) <= 40


async def test_many_pages_keep_whole_file_coverage_and_reject_changed_source(tmp_path):
    metadata = {}
    fp = tmp_path / "a.md"
    fp.write_text("\n".join(f"line {i}" for i in range(80)))
    note = TaskNote([{"role": "user", "content": "a.md 전체 분석"}], tmp_path, metadata)
    for offset in range(1, 81):
        await read(note, tmp_path, offset=offset, limit=1)
    note.accept(update(note))
    assert note.defects() == []
    restored = TaskNote([{"role": "user", "content": "a.md 전체 분석 계속"}], tmp_path, metadata)
    restored.accept(update(restored))
    assert restored.defects() == []
    fp.write_text("changed\n" + fp.read_text())
    stale = TaskNote([{"role": "user", "content": "a.md 전체 분석"}], tmp_path, metadata)
    stale.accept(update(stale))
    assert "coverage_unconfirmed:a.md" in stale.defects()


async def test_large_evidence_does_not_expand_model_guidance(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md 분석"}], tmp_path, {})
    (tmp_path / "a.md").write_text("\n".join(str(i) for i in range(80)))
    for offset in range(1, 81):
        await read(note, tmp_path, offset=offset, limit=1)
    response = json.loads(note.accept(update(note)))
    assert response["defects"] == []
    assert len(response["evidence"]) == 20
    cached = json.loads(note.guidance()["content"].split("\n")[-1])["cached_reads"]
    assert len(cached) == 20
