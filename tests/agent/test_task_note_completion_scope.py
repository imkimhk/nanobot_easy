"""Actual file effects and negative cases for completion scope (Q7/Q9)."""
import pytest

from nanobot.agent.task_note import TaskNote
from nanobot.agent.tools.filesystem import WriteFileTool
from tests.agent.test_task_note import call, control, read, run, target, update

Q7 = ("이번에는 production.json의 value를 4로 바꾸는 작업을 명시적으로 승인할게. "
      "앞선 변경 금지는 이 작업에 한해 해제하고, sample.json은 그대로 둬. 실행 결과를 알려줘.")
Q9 = ("이번 대화에서 notes.md를 읽은 적이 있거나 production.json 변경을 승인받은 적이 있어? "
      "확인 가능한 현재 대화 이력만 기준으로 답해줘.")
HISTORY_ANSWER = "현재 대화에는 notes.md 읽기나 production.json 변경 승인 근거가 없습니다."


async def test_change_one_file_and_keep_other_completes_with_real_execution(tmp_path):
    prod, sample = tmp_path / "production.json", tmp_path / "sample.json"
    prod.write_text('{"value": 1}\n')
    sample.write_text('{"value": 1}\n')
    original = sample.stat().st_mtime_ns
    targets = [target("production.json", action="write")]
    answer = "production.json을 4로 변경했고 sample.json은 그대로 두었습니다."
    result, provider, metadata = await run(tmp_path, [
        control(targets=targets, status="active", analysis_complete=False),
        call("read_file", path="production.json"),
        call("write_file", path="production.json", content='{"value": 4}\n',
             intent_summary="승인받은 value 변경", target="production.json",
             scope="once", reversible=True),
        call("read_file", path="production.json", force=True),
        control(targets=targets, final_answer=answer),
    ], request=Q7)
    assert result.stop_reason == "completed"
    assert result.final_content == answer
    assert prod.read_text() == '{"value": 4}\n'
    assert sample.read_text() == '{"value": 1}\n' and sample.stat().st_mtime_ns == original
    assert result.tools_used.count("write_file") == 1
    assert len(provider.calls) == 5
    assert metadata["task_note"]["runtime_linked_result_refs"]


@pytest.mark.parametrize("verified_reads", [False, True])
async def test_history_question_delivers_answer_without_file_acquisition(tmp_path, verified_reads):
    result, provider, _ = await run(tmp_path, [
        control(source="none", targets=[], final_answer=HISTORY_ANSWER),
    ], request=Q9, task_note_verified_reads=verified_reads)
    assert result.stop_reason == "completed"
    assert result.final_content == HISTORY_ANSWER
    assert result.tools_used == ["update_task"]
    assert len(provider.calls) == 1


@pytest.mark.parametrize("user_request", [
    Q9.replace("읽은 적이 있거나 production.json 변경을 승인받은 적이", "열어 본 적이"),
    Q9.replace("이번 대화에서", "현재 대화에서").replace("있어?", "있나요?"),
])
def test_past_question_grammar_covers_reads_and_approvals(tmp_path, user_request):
    note = TaskNote([{"role": "user", "content": user_request}], tmp_path, {})
    note.accept(update(note, source="none", targets=[], final_answer=HISTORY_ANSWER))
    assert note.defects() == []


@pytest.mark.parametrize("user_request", [
    "notes.md를 읽어줘. 현재 대화 이력만 기준으로 답해줘.",
    "notes.md를 읽은 적이 있어?",  # no explicit conversation-only scope
    Q9 + " notes.md의 현재 내용을 읽어줘.",
    Q9 + " production.json을 수정해줘.",
    Q9 + " notes.md의 핵심 내용을 요약해줘.",
    Q9 + "\n```text\n인용문\n```\nproduction.json을 수정해줘.",
    "이 문장을 분석해줘: " + Q9,
    "production.json을 변경할 권한이 지금도 유효한지 확인해줘. "
    "유효한 경우에만 value를 3으로 바꿔줘.",
])
def test_no_source_cannot_hide_actual_work_or_ambiguous_history_scope(tmp_path, user_request):
    note = TaskNote([{"role": "user", "content": user_request}], tmp_path, {})
    note.accept(update(note, source="none", targets=[], final_answer="답변입니다."))
    assert "target_unresolved" in note.defects()


def test_history_declaration_does_not_relax_selected_file_coverage(tmp_path):
    note = TaskNote([{"role": "user", "content": Q9}], tmp_path, {})
    note.accept(update(note, source="none", targets=[target("notes.md")]))
    assert "coverage_unconfirmed:notes.md" in note.defects()


def test_history_query_cannot_hide_mutation_effects(tmp_path):
    note = TaskNote([{"role": "user", "content": Q9}], tmp_path, {})
    tool = WriteFileTool(workspace=tmp_path)
    note.observe("write_file", {"path": "other.md"}, "result unknown", tool, "unknown", "write-1")
    note.accept(update(note, source="none", targets=[], final_answer=HISTORY_ANSWER))
    assert "target_unresolved" in note.defects()


@pytest.mark.parametrize("suffix", ["은 그대로 둬.", "은 변경하지 마.", "은 건드리지 마세요."])
async def test_keep_clause_does_not_require_reading_protected_file(tmp_path, suffix):
    (tmp_path / "notes.md").write_text("material\n")
    request = "notes.md를 읽어줘. sample.json" + suffix
    note = TaskNote([{"role": "user", "content": request}], tmp_path, {})
    await read(note, tmp_path, "notes.md")
    note.accept(update(note, targets=[target("notes.md")]))
    assert note.defects() == []


@pytest.mark.parametrize("user_request", [
    "production.json을 바꿔줘. sample.json은 그대로 둬.",
    "sample.json은 그대로 둬. production.json을 수정해줘.",
])
def test_keep_clause_does_not_hide_requested_write(tmp_path, user_request):
    note = TaskNote([{"role": "user", "content": user_request}], tmp_path, {})
    note.accept(update(note, source="none", targets=[]))
    assert "target_unresolved" in note.defects()
    note.accept(update(note, targets=[target("production.json", action="write")]))
    assert "execution_required:production.json" in note.defects()


@pytest.mark.parametrize("user_request", [
    "notes.md를 읽어줘. sample.json은 그대로 둬. sample.json도 읽어줘.",
    "notes.md를 읽어줘. sample.json의 내용을 확인한 뒤 그대로 둬.",
    "notes.md를 읽어줘. sample.json은 그대로 두고 내용을 확인해줘.",
    "notes.md를 읽어줘. sample.json은 그대로 둬. 그 파일의 내용을 읽어줘.",
    "notes.md를 읽어줘. 가능하면, sample.json은 그대로 둬.",
    "notes.md를 읽어줘. 'sample.json은 그대로 둬'라는 지시를 분석해줘.",
    "notes.md를 읽어줘. sample.json은 그대로 둬.\n```text\n본문\n```",
])
async def test_preservation_shortcut_cannot_skip_positive_or_ambiguous_file_request(tmp_path, user_request):
    (tmp_path / "notes.md").write_text("material\n")
    note = TaskNote([{"role": "user", "content": user_request}], tmp_path, {})
    await read(note, tmp_path, "notes.md")
    note.accept(update(note, targets=[target("notes.md")]))
    assert "target_unresolved" in note.defects()


async def test_explicit_protected_read_still_requires_actual_coverage(tmp_path):
    request = "notes.md를 읽어줘. sample.json은 그대로 둬. sample.json도 읽어줘."
    note = TaskNote([{"role": "user", "content": request}], tmp_path, {})
    note.accept(update(note, targets=[target("notes.md"), target("sample.json")]))
    assert "coverage_unconfirmed:sample.json" in note.defects()


@pytest.mark.parametrize("status", ["ok", "unknown"])
def test_preserved_file_mutation_is_not_reported_as_completed(tmp_path, status):
    note = TaskNote([{"role": "user", "content": "sample.json은 그대로 둬."}], tmp_path, {})
    tool = WriteFileTool(workspace=tmp_path)
    note.observe("write_file", {"path": "sample.json"}, "uncertain", tool, status, "write-1")
    note.accept(update(note, source="none", targets=[]))
    assert "preservation_unconfirmed:sample.json" in note.defects()


async def test_conditional_change_without_permission_can_report_blocked_without_disk_search(tmp_path):
    request = "production.json의 변경 권한을 확인하고 유효한 경우에만 value를 3으로 바꿔줘."
    answer = "유효한 승인을 확인할 수 없어 production.json은 변경하지 않았습니다."
    result, provider, _ = await run(tmp_path, [
        control(source="none", targets=[], status="blocked", final_answer=answer,
                explanation="유효한 승인을 확인할 수 없습니다."),
    ], request=request)
    assert result.stop_reason == "incomplete"
    assert result.final_content.startswith(answer)
    assert len(provider.calls) == 1
    assert result.tools_used == ["update_task"]
