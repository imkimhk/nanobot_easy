"""A real multi-file patch must satisfy the runner's file completion contract."""

import hashlib

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.task_note import TaskNote
from nanobot.agent.tools.apply_patch import ApplyPatchTool
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.filesystem import WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from tests.agent.test_task_note import Script, call, control, target, update


def edits():
    return [{"path": path, "action": "add", "new_text": "saved"}
            for path in ("a.md", "b.md")]


async def test_runner_completes_after_real_multi_file_patch(tmp_path):
    tools = ToolRegistry()
    tools.register(ApplyPatchTool(workspace=tmp_path))
    targets = [target(path, action="write") for path in ("a.md", "b.md")]
    provider = Script([
        control(targets=targets, status="active", analysis_complete=False),
        call("apply_patch", edits=edits(), intent_summary="Save the two requested files",
             target="a.md, b.md", scope="once", reversible=True),
        control(targets=targets, answer="두 파일을 저장했습니다."),
    ])
    metadata = {}
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "a.md와 b.md에 saved를 저장해줘."}],
        tools=tools, model="test", workspace=tmp_path, max_iterations=3,
        max_tool_result_chars=8000,
        task_note_enabled=True, task_note_metadata=metadata,
    ))
    assert result.stop_reason == "completed", metadata["task_note"]
    assert result.final_content == "두 파일을 저장했습니다."
    assert len(provider.calls) == 3
    assert result.tools_used.count("apply_patch") == 1
    note = metadata["task_note"]
    writes = [e for e in note["events"] if e["name"] == "apply_patch"]
    assert {e["target"] for e in writes} == {str(tmp_path / p) for p in ("a.md", "b.md")}
    assert note["runtime_linked_result_refs"] == ["apply_patch"]
    assert note["update"]["result_refs"] == ["apply_patch"]
    assert note["metrics"]["tool_calls"] == 1
    for path in ("a.md", "b.md"):
        assert (tmp_path / path).read_text() == "saved\n"


@pytest.mark.parametrize("mode", ["dry_run", "failed", "untrusted", "unknown"])
async def test_patch_without_successful_trusted_receipts_cannot_complete(tmp_path, mode):
    note = TaskNote([{"role": "user", "content": "a.md와 b.md를 저장해줘."}], tmp_path, {})
    targets = [target(p, action="write") for p in ("a.md", "b.md")]
    note.accept(update(note, targets=targets, status="active", analysis_complete=False))
    tool = ApplyPatchTool(workspace=tmp_path)
    args = {"edits": edits()}
    status = "ok"
    if mode == "dry_run":
        args["dry_run"] = True
    if mode == "failed":
        args["edits"][1] = {"path": "b.md", "action": "replace",
                            "old_text": "absent", "new_text": "changed"}
        status = "error"
    result = await tool.execute(**args)
    if mode == "untrusted":
        tool = WriteFileTool(workspace=tmp_path)
        result = ToolResult("Patch applied: a.md, b.md")
        result.write_evidence = [{"target": str(tmp_path / p), "sha256": "a" * 64}
                                 for p in ("a.md", "b.md")]
    if mode == "unknown":
        status = "unknown"
    note.observe("apply_patch", args, result, tool, status, "patch-call")
    note.accept(update(note, targets=targets))
    assert {"execution_required:a.md", "execution_required:b.md"}.issubset(note.defects())
    if mode in {"dry_run", "failed"}:
        assert not (tmp_path / "a.md").exists()
    if mode == "dry_run":
        assert not note.events[-1]["mutating"]


async def test_patch_receipt_tracks_final_bytes_once_per_resolved_file(tmp_path):
    path = tmp_path / "a.md"
    path.write_bytes(b"first\r\n")
    tool = ApplyPatchTool(workspace=tmp_path)
    args = {"edits": [
        {"path": "a.md", "action": "add", "new_text": "second"},
        {"path": "./a.md", "action": "add", "new_text": "third"},
    ]}
    result = await tool.execute(**args)
    assert result.write_evidence == [{"target": str(path),
                                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
    assert path.read_bytes() == b"first\r\nsecond\r\nthird\r\n"
    note = TaskNote([{"role": "user", "content": "a.md를 수정해줘."}], tmp_path, {})
    note.accept(update(note, targets=[target(action="write")], status="active"))
    note.observe("apply_patch", args, result, tool, "ok", "patch-call")
    assert len(note.write_results()) == 1
    assert note.mutation_error("apply_patch", args)
    assert note.mutation_error("apply_patch", {**args, "dry_run": True}) is None
