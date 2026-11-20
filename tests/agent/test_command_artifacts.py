"""Command-generated deliverables need runtime evidence, not another rewrite."""

import shlex
import sys

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.task_note import Target, TaskNote, active_task_note
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.filesystem import WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from tests.agent.test_task_note import Script, call, control, target, update, write_args


def command(code):
    return shlex.join([sys.executable, "-c", code])


async def test_generated_binary_and_text_finish_in_three_model_calls(tmp_path):
    tools = ToolRegistry()
    tools.register(ExecTool(working_dir=str(tmp_path)))
    targets = [target("report.pdf", action="write"), target("report.md", action="write")]
    provider = Script([
        control(targets=targets, source="none", status="active", analysis_complete=False),
        call("exec", command=command(
            "from pathlib import Path; Path('report.pdf').write_bytes(b'%PDF-1.4\\nreport'); "
            "Path('report.md').write_text('Report with sources')"),
            intent_summary="Create the two declared report files", reversible=True,
            scope="once", target=str(tmp_path)),
        control(targets=targets, source="none", answer="PDF와 원문을 저장했습니다."),
    ])
    metadata = {}
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "PDF 보고서와 원문을 작성하고 검토해 줘"}],
        tools=tools, model="test", max_iterations=3, max_tool_result_chars=8000,
        workspace=tmp_path, task_note_enabled=True, task_note_metadata=metadata,
    ))
    assert result.stop_reason == "completed", (result.final_content, result.tool_events)
    assert result.final_content == "PDF와 원문을 저장했습니다."
    assert len(provider.calls) == 3
    assert result.tools_used.count("exec") == 1
    receipts = metadata["task_note"]["events"]
    assert {e["target"] for e in receipts} == {str(tmp_path / "report.pdf"), str(tmp_path / "report.md")}
    assert all(e["expected_sha256"] for e in receipts)
    assert not any("Completion defects" in str(m.get("content")) for m in result.messages)


async def test_real_earlier_refs_do_not_prevent_linking_latest_file_write(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md를 저장해 줘"}], tmp_path, {})
    tool = ExecTool(working_dir=str(tmp_path))
    ok = ToolResult("command finished")
    ok.execution_evidence = {"exit_code": 0}
    note.observe("exec", {"command": "first command"}, ok, tool, "ok", "earlier-command")
    note.observe("exec", {"command": "later command"}, ok, tool, "ok", "later-command")
    writer = WriteFileTool(workspace=tmp_path)
    note.observe("write_file", write_args(), "saved", writer, "ok", "actual-write")
    note.accept(update(note, targets=[target(action="write")], result_refs=["earlier-command"]))
    assert note.defects() == []
    assert note.update.result_refs == ["earlier-command", "actual-write"]


@pytest.mark.parametrize("code,changed", [
    ("print('Successfully wrote report.pdf')", False),
    ("from pathlib import Path; Path('report.pdf').read_bytes()", False),
    ("from pathlib import Path; Path('report.pdf').write_bytes(b'changed'); raise SystemExit(1)", True),
])
async def test_output_existing_file_and_failed_command_cannot_claim_success(tmp_path, code, changed):
    path = tmp_path / "report.pdf"
    path.write_bytes(b"original")
    note = TaskNote([{"role": "user", "content": "report.pdf를 작성해 줘"}], tmp_path, {})
    note.accept(update(note, targets=[target("report.pdf", action="write")], status="active"))
    tool = ExecTool(working_dir=str(tmp_path))
    token = active_task_note.set(note)
    try:
        result = await tool.execute(command=command(code))
    finally:
        active_task_note.reset(token)
    status = "error" if getattr(result, "is_error", False) else "ok"
    note.observe("exec", {"command": command(code)}, result, tool, status, "command")
    note.accept(update(note, targets=[target("report.pdf", action="write")], result_refs=[]))
    assert not getattr(result, "write_evidence", [])
    assert note.defects() == ["execution_required:report.pdf"]
    assert (path.read_bytes() != b"original") == changed


@pytest.mark.parametrize("name", ["../outside.pdf", ".secrets/private.pdf", "directory"])
def test_artifact_capture_respects_boundaries_and_regular_files(tmp_path, name):
    from nanobot.agent.artifact_evidence import snapshot_writes

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "directory").mkdir()
    targets = [Target.model_validate(target(name, action="write"))]
    assert snapshot_writes(targets, workspace, workspace) == []


def test_symlink_retarget_and_unselected_output_cannot_supply_evidence(tmp_path):
    from nanobot.agent.artifact_evidence import changed_writes, snapshot_writes

    original, replacement = tmp_path / "original.pdf", tmp_path / "replacement.pdf"
    original.write_bytes(b"original")
    replacement.write_bytes(b"replacement")
    alias = tmp_path / "report.pdf"
    try:
        alias.symlink_to(original)
    except OSError:
        pytest.skip("symlinks unavailable")
    snapshots = snapshot_writes([Target.model_validate(target("report.pdf", action="write"))],
                               tmp_path, tmp_path)
    alias.unlink()
    alias.symlink_to(replacement)
    (tmp_path / "unselected.pdf").write_bytes(b"new output")
    assert changed_writes(snapshots, tmp_path, tmp_path) == []
