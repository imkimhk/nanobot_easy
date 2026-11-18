"""exec(background=true) jobs, exec_job status/kill, and the foreground timeout note."""

from __future__ import annotations

import asyncio
import os
import re
import sys

import pytest

from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context
from nanobot.agent.tools.exec_job import ExecJobManager, ExecJobTool
from nanobot.agent.tools.registry import ToolRegistry, is_tool_error_result
from nanobot.agent.tools.shell import ExecTool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")


def _job_id(text: str) -> str:
    match = re.search(r"job_id: ([0-9a-f]+)", text)
    assert match, text
    return match.group(1)


async def _wait_state(tool: ExecJobTool, job_id: str, state: str, timeout: float = 5.0) -> str:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        status = await tool.execute(action="status", job_id=job_id)
        if f"state: {state}" in status or asyncio.get_running_loop().time() > deadline:
            return status
        await asyncio.sleep(0.05)


async def test_background_job_reports_running_then_exit_code_and_log_tail(tmp_path):
    manager = ExecJobManager()
    exec_tool = ExecTool(working_dir=str(tmp_path), job_manager=manager)
    jobs = ExecJobTool(manager=manager, workspace=str(tmp_path))

    started = await exec_tool.execute(command="echo start; sleep 0.5; echo finish; exit 3", background=True)
    job_id = _job_id(started)

    assert "Background job started" in started
    assert "state: running" in await jobs.execute(action="status", job_id=job_id)
    status = await _wait_state(jobs, job_id, "exited")
    assert "exit_code: 3" in status
    assert "start" in status and "finish" in status
    log = tmp_path / ".nanobot" / "exec-jobs" / f"{job_id}.log"
    assert log.read_text().splitlines() == ["start", "finish"]
    assert job_id in await jobs.execute(action="list")


async def test_kill_terminates_the_job_process_group(tmp_path):
    manager = ExecJobManager()
    exec_tool = ExecTool(working_dir=str(tmp_path), job_manager=manager)
    jobs = ExecJobTool(manager=manager, workspace=str(tmp_path))

    job_id = _job_id(await exec_tool.execute(command="sleep 30 & sleep 30; wait", background=True))
    pid = manager.get(job_id).pid
    status = await jobs.execute(action="kill", job_id=job_id)

    assert "state: killed" in status
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


async def test_background_job_has_its_own_hard_timeout(tmp_path):
    manager = ExecJobManager()
    exec_tool = ExecTool(working_dir=str(tmp_path), job_manager=manager, background_timeout=1)
    jobs = ExecJobTool(manager=manager, workspace=str(tmp_path))

    job_id = _job_id(await exec_tool.execute(command="sleep 30", background=True))
    status = await _wait_state(jobs, job_id, "timed_out", timeout=8)

    assert "state: timed_out" in status
    assert "may have partially completed" in status


async def test_background_start_keeps_foreground_guards(tmp_path):
    manager = ExecJobManager()
    exec_tool = ExecTool(working_dir=str(tmp_path), job_manager=manager, restrict_to_workspace=True)

    denied = await exec_tool.execute(command="rm -rf build", background=True)
    outside = await exec_tool.execute(command="ls", working_dir="/", background=True)

    assert is_tool_error_result("exec", denied) and "deny pattern" in denied
    assert is_tool_error_result("exec", outside) and "outside the configured workspace" in outside
    assert manager.list() == []


async def test_jobs_are_scoped_to_the_starting_session(tmp_path):
    manager = ExecJobManager()
    exec_tool = ExecTool(working_dir=str(tmp_path), job_manager=manager)
    jobs = ExecJobTool(manager=manager, workspace=str(tmp_path))
    token = bind_request_context(RequestContext(channel="telegram", chat_id="1", session_key="a"))
    try:
        job_id = _job_id(await exec_tool.execute(command="true", background=True))
    finally:
        reset_request_context(token)
    token = bind_request_context(RequestContext(channel="telegram", chat_id="2", session_key="b"))
    try:
        assert manager.get(job_id, "b") is None
        assert await jobs.execute(action="list") == "No background jobs."
    finally:
        reset_request_context(token)


async def test_status_of_job_from_earlier_runtime_uses_metadata_file(tmp_path):
    first = ExecJobManager()
    exec_tool = ExecTool(working_dir=str(tmp_path), job_manager=first)
    jobs = ExecJobTool(manager=first, workspace=str(tmp_path))
    job_id = _job_id(await exec_tool.execute(command="echo done", background=True))
    await _wait_state(jobs, job_id, "exited")

    restarted = ExecJobTool(manager=ExecJobManager(), workspace=str(tmp_path))
    status = await restarted.execute(action="status", job_id=job_id)

    assert "state: exited" in status and "exit_code: 0" in status and "done" in status
    missing = await restarted.execute(action="status", job_id="../../etc")
    assert is_tool_error_result("exec_job", missing)


async def test_foreground_timeout_tells_model_to_verify_before_retry(tmp_path):
    result = await ExecTool(working_dir=str(tmp_path)).execute(command="sleep 5", timeout=1)

    assert is_tool_error_result("exec", result)
    assert "timed out after 1 seconds" in result
    assert "may have partially or fully completed" in result
    assert "Do not rerun it automatically" in result
    assert "background=true" in result


async def test_short_foreground_command_behavior_is_unchanged(tmp_path):
    result = await ExecTool(working_dir=str(tmp_path)).execute(command="echo hello")

    assert "hello" in result and "Exit code: 0" in result
    assert "job_id" not in result
    assert ExecTool()._resolve_timeout(None) == 60
    assert ExecTool()._resolve_timeout(900) == 600


def test_exec_job_kill_requires_intent_metadata_but_status_does_not():
    assert ToolRegistry._requires_intent_metadata("exec_job", {"action": "kill"}) is True
    assert ToolRegistry._requires_intent_metadata("exec_job", {"action": "status"}) is False
    assert ToolRegistry._requires_intent_metadata("exec", {"command": "x", "background": True}) is True
