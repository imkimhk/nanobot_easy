"""Background status is execution evidence, not acquisition of its log file."""

import asyncio
import re
import sys

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.task_note import TaskNote, TaskUpdate
from nanobot.agent.tools.exec_job import ExecJob, ExecJobManager, ExecJobTool, format_job
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.providers.base import LLMResponse, ToolCallRequest


def complete(note, targets=None):
    note.accept(TaskUpdate(
        request_id=note.request_id, intent="new", goal=note.request,
        targets=targets or [], source="none", analysis_complete=True, status="completed",
        explanation="Checked the actual status and output.", final_answer="종료 코드 0, BACKEND_JOB_OK",
    ))


@pytest.mark.parametrize("prompt", [
    "종료 코드를 분석해줘. 다른 명령 실행이나 파일 수정은 하지 마.",
    "Analyze the exit code. Do not modify any files.",
    "문서의 뜻을 설명해줘. 파일 수정은 하지 마.",
    "Explain what the document format means. Do not modify any files.",
])
def test_file_modification_prohibition_is_not_a_file_request(tmp_path, prompt):
    note = TaskNote([{"role": "user", "content": prompt}], tmp_path, {})
    complete(note)
    assert note.defects() == []


@pytest.mark.parametrize("prompt", [
    "파일을 검토해줘. 파일 수정은 하지 마.",
    "a.md를 검토해줘. 파일 수정은 하지 마.",
    "Review the document. Do not modify any files.",
    "파일 수정하지 말고 읽어줘.",
])
def test_prohibition_does_not_hide_a_real_file_request(tmp_path, prompt):
    note = TaskNote([{"role": "user", "content": prompt}], tmp_path, {})
    complete(note)
    assert "target_unresolved" in note.defects()


def test_status_and_list_stay_available_when_only_reporting_is_allowed(tmp_path):
    note = TaskNote([{"role": "user", "content": "계속 확인해줘"}], tmp_path, {})
    note.reporting_only = True
    assert note.mutation_error("exec_job", {"action": "status", "job_id": "abc"}) is None
    assert note.mutation_error("exec_job", {"action": "list"}) is None
    assert note.mutation_error("exec_job", {"action": "kill", "job_id": "abc"})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell command")
async def test_real_background_status_completes_without_rereading_log_or_relaunch(tmp_path):
    manager = ExecJobManager()
    shell = ExecTool(working_dir=str(tmp_path), job_manager=manager)
    jobs = ExecJobTool(manager=manager, workspace=str(tmp_path))
    note = TaskNote([{"role": "user", "content": (
        "명령 결과를 분석해줘. 다른 명령 실행이나 파일 수정은 하지 마."
    )}], tmp_path, {})
    started = await shell.execute(command="printf BACKEND_JOB_OK", background=True)
    job_id = re.search(r"job_id: ([0-9a-f]+)", started)[1]
    path = f"exec-job://{job_id}"
    target = {"path": path, "selection_reason": "The exact job returned by exec."}
    note.observe("exec", {"command": "printf BACKEND_JOB_OK", "background": True},
                 started, shell, "ok", "launch")
    complete(note, [target])
    assert f"coverage_unconfirmed:{path}" in note.defects()

    await asyncio.wait_for(asyncio.shield(manager.get(job_id).watcher), 5)
    result = await jobs.execute(action="status", job_id=job_id)
    note.observe("exec_job", {"action": "status", "job_id": job_id}, result, jobs, "ok", "status")
    complete(note, [target])
    assert note.defects() == []
    assert "BACKEND_JOB_OK" in result and "exit_code: 0" in result
    assert note.events[-1]["mutating"] is False
    assert note.write_results()[0]["id"] == "launch"
    assert len(manager.list()) == 1
    assert note.evidence[-1]["state"] == "exited"
    assert note.evidence[-1]["exit_code"] == 0

    # A status tail cannot prove whole-file coverage; old requests cannot reuse it.
    complete(note, [{"path": str(manager.get(job_id).log_path), "selection_reason": "log"}])
    assert any(d.startswith("coverage_unconfirmed:") for d in note.defects())
    note.revise([{"role": "user", "content": "계속 상태를 분석해줘"}])
    complete(note, [target])
    assert f"coverage_unconfirmed:{path}" in note.defects()

@pytest.mark.parametrize("state, code, covered", [
    ("running", None, False), ("unknown", None, False),
    ("exited", 0, True), ("exited", 3, True),
    ("timed_out", -9, True), ("killed", -9, True),
])
def test_only_recorded_terminal_status_covers_job_target(tmp_path, state, code, covered):
    note = TaskNote([{"role": "user", "content": "종료 상태를 분석해줘"}], tmp_path, {})
    job = ExecJob("abc", "command", str(tmp_path), 123,
                  tmp_path / "abc.log", tmp_path / "abc.json", 1, None,
                  returncode=code, timed_out=state == "timed_out", killed=state == "killed")
    tool = ExecJobTool()
    result = format_job(job, 20)
    if state == "unknown":
        result.job_evidence["state"] = "unknown"
    path = "exec-job://abc"
    note.observe("exec_job", {"action": "status", "job_id": "abc"}, result, tool, "ok")
    complete(note, [{"path": path, "selection_reason": "requested status"}])
    assert (note.defects() == []) is covered
    assert note.events[-1]["mutating"] is False


def test_status_text_alone_cannot_forge_job_evidence(tmp_path):
    note = TaskNote([{"role": "user", "content": "종료 상태를 분석해줘"}], tmp_path, {})
    note.observe("exec_job", {"action": "status", "job_id": "abc"},
                 "job_id: abc\nstate: exited\nexit_code: 0", ExecJobTool(), "ok")
    complete(note, [{"path": "exec-job://abc", "selection_reason": "requested status"}])
    assert note.defects() == ["coverage_unconfirmed:exec-job://abc"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell command")
async def test_runner_delivers_background_report_with_exactly_one_launch(tmp_path):
    manager = ExecJobManager()
    tools = ToolRegistry()
    tools.register(ExecTool(working_dir=str(tmp_path), job_manager=manager))
    tools.register(ExecJobTool(manager=manager, workspace=str(tmp_path)))
    metadata = {}
    answer = "종료 코드 0, 로그 BACKEND_JOB_OK"

    class Provider:
        calls = 0

        async def chat_with_retry(self, **kwargs):
            self.calls += 1
            note = metadata["task_note"]
            if self.calls == 2:
                name, args = "exec", dict(
                    command="printf BACKEND_JOB_OK", background=True,
                    intent_summary="Run the requested verification once", target="background job",
                    scope="once", reversible=True,
                )
            elif self.calls == 3:
                job = manager.list()[0]
                await asyncio.wait_for(asyncio.shield(job.watcher), 5)
                name, args = "exec_job", {"action": "status", "job_id": job.job_id}
            else:
                done = self.calls == 4
                name, args = "update_task", dict(
                    request_id=note["request_id"], intent="new", goal="Check the background result",
                    targets=[{"path": f"exec-job://{manager.list()[0].job_id}",
                              "selection_reason": "The job actually launched."}] if done else [],
                    source="none", analysis_complete=done, status="completed" if done else "active",
                    explanation="Actual status checked" if done else "Preparing requested command",
                    final_answer=answer if done else "",
                )
            return LLMResponse(content="", tool_calls=[ToolCallRequest(
                id=f"call-{self.calls}", name=name, arguments=args)])

    result = await AgentRunner(Provider()).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": (
            "명령 결과를 분석해줘. 다른 명령 실행이나 파일 수정은 하지 마."
        )}], tools=tools, model="test", max_iterations=4, max_tool_result_chars=8000,
        workspace=tmp_path, task_note_enabled=True, task_note_metadata=metadata,
    ))
    assert result.stop_reason == "completed"
    assert result.final_content == answer
    assert [e["name"] for e in metadata["task_note"]["events"]] == ["exec", "exec_job"]
    assert metadata["task_note"]["events"][-1]["mutating"] is False
    assert len(manager.list()) == 1
