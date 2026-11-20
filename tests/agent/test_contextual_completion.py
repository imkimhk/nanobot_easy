"""Completion must separate request scope from tool-backed execution evidence."""

import pytest

from nanobot.agent.task_note import TaskNote
from nanobot.agent.tools.apply_patch import ApplyPatchTool
from tests.agent.test_task_note import call, control, read, run, target, update

REPORT_REQUEST = (
    "이 검토 결과의 원인을 확인해줘. 검토 보고서: data.py만 읽었는데 a.py도 처리된 것으로 "
    "통과했고 audit.py도 같은 사례입니다. 실제 검토 대상은 checker.py입니다."
)
EXAMPLES = ["data.py", "a.py", "audit.py"]


async def test_example_names_can_be_resolved_without_reading_unrelated_files(tmp_path):
    (tmp_path / "checker.py").write_text("result = 1\n")
    result, provider, _ = await run(tmp_path, [
        control(targets=[target("checker.py")], status="active", analysis_complete=False,
                contextual_files=EXAMPLES),
        # The selected implementation is still actually read; examples need no acquisition.
        call("read_file", path="checker.py"),
        control(targets=[target("checker.py")], contextual_files=EXAMPLES,
                answer="검토 결과: checker.py의 판정 조건을 확인했습니다."),
    ], request=REPORT_REQUEST, max_iterations=3)
    assert result.stop_reason == "completed"
    assert len(provider.calls) == 3
    assert result.tools_used.count("read_file") == 1
    assert not any((tmp_path / p).exists() for p in EXAMPLES)


@pytest.mark.parametrize("prompt", [
    "a.py와 data.py를 읽어줘.",
    "Read a.py and data.py.",
    "a.py의 내용을 알려줘.",
    "What does a.py contain?",
    "What is in a.py?",
])
async def test_contextual_declaration_cannot_waive_explicit_file_work(tmp_path, prompt):
    (tmp_path / "data.py").write_text("unrelated = True\n")
    note = TaskNote([{"role": "user", "content": prompt}], tmp_path, {})
    await read(note, tmp_path, "data.py")
    note.accept(update(note, targets=[target("data.py")], contextual_files=["a.py"]))
    assert {"kind": "requested_file_not_selected", "path": "a.py"} in note.scope_conflicts()
    assert "target_unresolved" in note.defects()


@pytest.mark.parametrize("remaining", ["scope", "coverage", "execution"])
async def test_success_refs_link_independently_of_other_completion_defects(tmp_path, remaining):
    request = "saved.py를 저장해줘."
    if remaining == "scope":
        request += " 참고 사례는 sample.py입니다."
    note = TaskNote([{"role": "user", "content": request}], tmp_path, {})
    targets = [target("saved.py", action="write")]
    if remaining == "coverage":
        targets.append(target("unread.py"))
    if remaining == "execution":
        targets.append(target("not-written.py", action="write"))
    note.accept(update(note, targets=targets, status="active", analysis_complete=False))
    tool = ApplyPatchTool(workspace=tmp_path)
    args = {"edits": [{"path": "saved.py", "action": "add", "new_text": "saved = True"}]}
    receipt = await tool.execute(**args)
    note.observe("apply_patch", args, receipt, tool, "ok", "real-patch")
    note.accept(update(note, targets=targets, result_refs=[]))
    assert "real-patch" in note.update.result_refs
    assert not any(d.startswith("answer_correction:saved.py") for d in note.defects())
    assert {"scope": "target_unresolved", "coverage": "coverage_unconfirmed:unread.py",
            "execution": "execution_required:not-written.py"}[remaining] in note.defects()


async def test_reference_resolution_recovers_without_repeating_completed_mutation(tmp_path):
    # Neutral mention wording leaves its role for the model, independent of language keywords.
    request = "changed.py를 수정해줘. 보고서의 재현 사례: sample.py."
    result, provider, metadata = await run(tmp_path, [
        control(targets=[target("changed.py", action="write")], status="active",
                analysis_complete=False),
        call("write_file", path="changed.py", content="saved\n", intent_summary="Save requested file",
            target="changed.py", scope="once", reversible=True),
        control(targets=[target("changed.py", action="write")], result_refs=[],
                answer="수정 결과: changed.py를 저장했습니다."),
        control(targets=[target("changed.py", action="write")], result_refs=[],
                contextual_files=["sample.py"], answer="수정 결과: changed.py를 저장했습니다."),
    ], request=request, max_iterations=4)
    assert result.stop_reason == "completed"
    assert len(provider.calls) == 4
    assert result.tools_used.count("write_file") == 1
    assert metadata["task_note"]["update"]["result_refs"] == ["write_file"]
    assert not (tmp_path / "sample.py").exists()
