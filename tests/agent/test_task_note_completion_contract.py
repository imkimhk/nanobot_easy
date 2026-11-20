"""Small counterexamples for scope inference versus actual execution evidence."""
import json

import pytest

from nanobot.agent.task_note import TaskNote
from nanobot.agent.tools.filesystem import WriteFileTool
from tests.agent.test_task_note import control, read, run, target, update


@pytest.mark.parametrize("prompt,source", [
    ("Analyze the exit code. Do not edit the file.", "none"),
    ("Analyze the result. Do not read the document.", "none"),
    ("Review the document below.\n본문: 매출은 10이고 비용은 7이다.", "provided"),
    ("Review the document below. Do not edit the file.\n본문: 검토할 본문입니다.", "provided"),
])
async def test_english_restrictions_and_pasted_documents_finish_without_file_work(
        tmp_path, prompt, source):
    report = "분석 결과: 제공된 내용만 검토했으며 파일 작업은 하지 않았습니다."
    result, provider, _ = await run(tmp_path, [
        control(source=source, targets=[], final_answer=report),
    ], request=prompt, max_iterations=1)
    assert result.stop_reason == "completed"
    assert result.final_content == report
    assert result.tools_used == ["update_task"]
    assert len(provider.calls) == 1


def test_generic_document_still_requires_targets_when_file_scope_is_selected(tmp_path):
    note = TaskNote([{"role": "user", "content": "Review the document."}], tmp_path, {})
    note.accept(update(note, source="files", targets=[]))
    assert "target_unresolved" in note.defects()
    assert {"kind": "declared_file_source_without_target"} in note.scope_conflicts()


async def test_research_and_concept_paraphrases_deliver_without_invented_file_work(tmp_path):
    requests = [
        "다른 에이전트는 웹에서 문서와 사용기를 모으고 장단점을 비교해서 보고해줘.",
        "다른 에이전트는 웹에서 자료와 사용기를 모으고 장단점을 비교해서 보고해줘.",
        "파일 시스템과 데이터베이스의 장단점을 비교해줘.",
        "저장 시스템과 데이터베이스의 장단점을 비교해줘.",
        "문서화 전략의 장단점을 비교해줘.",
        "기록 전략의 장단점을 비교해줘.",
    ]
    report = "비교 결과: 첫 방식은 단순하고 두 번째 방식은 확장에 유리합니다."
    for request in requests:
        result, provider, _ = await run(tmp_path, [
            control(source="none", targets=[], final_answer=report),
        ], request=request, max_iterations=1)
        assert result.stop_reason == "completed"
        assert result.final_content == report
        assert result.tools_used == ["update_task"]
        assert len(provider.calls) == 1


async def test_reference_names_and_history_are_not_acquisition_requests(tmp_path):
    for request in [
        "README.md라는 파일명의 역할과 .md 확장자의 의미를 비교해줘.",
        "notes.md를 읽은 적이 있어? 현재 대화에서만 확인해줘.",
        "공식 웹 문서 https://example.org/guide.md를 읽어 비교해줘.",
        "설정의 장단점을 분석해줘. 파일을 수정하지 마.",
    ]:
        note = TaskNote([{"role": "user", "content": request}], tmp_path, {})
        note.accept(update(note, source="none", targets=[]))
        assert note.defects() == []
        assert note.events == []


def test_explicit_file_operations_cannot_be_waived_by_declaring_no_source(tmp_path):
    for request in [
        "a.md 파일 전체를 검토해줘.", "a.md와 b.md를 읽고 비교해줘.",
        "Review a.md and b.md.", "그 설계서 파일을 검토해줘.",
        "a.md를 수정해줘.",
        "a.md의 내용을 알려줘.", "What does a.md contain?",
    ]:
        note = TaskNote([{"role": "user", "content": request}], tmp_path, {})
        response = json.loads(note.accept(update(note, source="none", targets=[])))
        assert "target_unresolved" in response["defects"]
        assert response["scope_conflicts"]


async def test_selected_file_still_requires_full_actual_read_coverage(tmp_path):
    (tmp_path / "a.md").write_text("one\ntwo\nthree\n")
    note = TaskNote([{"role": "user", "content": "a.md 전체를 읽어줘."}], tmp_path, {})
    note.accept(update(note))
    await read(note, tmp_path, limit=1)
    note.accept(update(note))
    assert "coverage_unconfirmed:a.md" in note.defects()
    await read(note, tmp_path, offset=2, limit=2)
    note.accept(update(note))
    assert note.defects() == []


def test_write_denial_unknown_and_duplicate_guards_stay_effective(tmp_path):
    note = TaskNote([{"role": "user", "content": "a.md를 수정해줘."}], tmp_path, {})
    args = {"path": "a.md", "content": "new"}
    assert note.mutation_error("write_file", args)
    note.accept(update(note, targets=[target(action="write")]))
    assert "execution_required:a.md" in note.defects()
    for status, result, defect in [
        ("error", "permission denied", "blocked:a.md"),
        ("error", "timeout; result unknown", "record_check:a.md"),
        ("ok", "saved", None),
    ]:
        note.events.clear()
        note.observe("write_file", args, result, WriteFileTool(workspace=tmp_path), status, "w1")
        note.accept(update(note, targets=[target(action="write")]))
        assert note.mutation_error("write_file", args)
        assert (defect in note.defects()) if defect else note.defects() == []
    note.revise([{"role": "user", "content": "수정하지 말고 검토만 해."}])
    note.accept(update(note, targets=[target(action="write")]))
    assert note.mutation_error("write_file", args)


async def test_scope_recovery_corrects_selection_without_research_or_extra_reads(tmp_path):
    report = "문서화 전략 비교 결과: 자동 생성은 일관성, 수동 작성은 설명의 유연성이 장점입니다."
    result, provider, _ = await run(tmp_path, [
        control(source="files", targets=[], final_answer=report),
        control(source="none", targets=[], final_answer=report),
    ], request="문서화 전략을 비교해줘.")
    assert result.stop_reason == "completed"
    assert result.final_content == report
    assert len(provider.calls) == 2
    assert result.tools_used == ["update_task", "update_task"]
    assert any("request/selection disagreement" in str(m.get("content", ""))
               for m in provider.calls[1]["messages"])


async def test_unverified_execution_cannot_deliver_a_false_success_draft(tmp_path):
    report = "a.md 변경을 완료했습니다."
    result, provider, _ = await run(tmp_path, [
        control(targets=[target(action="write")], final_answer=report),
    ], request="a.md를 수정해줘.", max_iterations=1)
    assert result.stop_reason == "incomplete"
    assert report not in result.final_content
    assert "execution_required:a.md" in result.final_content
    assert len(provider.calls) == 1
    assert not (tmp_path / "a.md").exists()


async def test_blocked_scope_delivers_known_findings_without_repeating_work(tmp_path):
    report = "비교 기준은 정리했습니다. 대상 설계서의 버전을 확인할 수 없어 내용 비교는 남았습니다."
    result, provider, _ = await run(tmp_path, [
        control(source="none", targets=[], final_answer=report),
        control(source="none", targets=[], status="partial", final_answer=report,
                explanation="비교할 설계서의 버전 확인 필요"),
    ], request="그 설계서 파일을 검토해줘.")
    assert result.stop_reason == "incomplete"
    assert report in result.final_content
    assert "버전 확인 필요" in result.final_content
    assert len(provider.calls) == 2
    assert result.tools_used == ["update_task", "update_task"]


def test_selected_filename_must_match_the_whole_name(tmp_path):
    note = TaskNote(
        [{"role": "user", "content": "a.py와 data.py를 수정해줘."}],
        tmp_path,
        {},
    )
    note.accept(update(note, targets=[target(path=str(tmp_path / "data.py"), action="write")]))
    assert {"kind": "requested_file_not_selected", "path": "a.py"} in note.scope_conflicts()

    audit = TaskNote(
        [{"role": "user", "content": "audit.py와 test_action_audit.py를 수정해줘."}],
        tmp_path,
        {},
    )
    audit.accept(update(
        audit,
        targets=[target(path=str(tmp_path / "test_action_audit.py"), action="write")],
    ))
    assert {"kind": "requested_file_not_selected", "path": "audit.py"} in audit.scope_conflicts()

    exact = TaskNote(
        [{"role": "user", "content": "data.py를 수정해줘."}],
        tmp_path,
        {},
    )
    exact.accept(update(exact, targets=[target(path=str(tmp_path / "data.py"), action="write")]))
    assert exact.scope_conflicts() == []
