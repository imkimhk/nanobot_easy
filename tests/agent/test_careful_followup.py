"""Protect existing behavior while repairing the follow-up review counterexamples."""

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.task_note import needs_note


@pytest.mark.parametrize("prompt", [
    "설정 방법을 설명하고 변경해줘",
    "삭제 방법을 설명하고 삭제해줘",
    "실행 방법을 설명하고 실행해줘",
    "Explain the procedure and fix the settings",
])
def test_advice_with_explicit_action_still_tracks_execution(prompt):
    assert needs_note([{"role": "user", "content": prompt}])


@pytest.mark.parametrize("prompt", [
    "설정 변경 방법만 설명해줘. 실제 변경하지 마.",
    "삭제 방법만 설명해줘. 삭제하지 마.",
    "명령 실행 방법만 설명해줘. 실행하지 마.",
    "How do I change the settings? Explain only; do not change anything.",
    "대한민국의 수도는 어디야? 도구를 사용하지 말고 한 문장으로 답해줘.",
])
def test_explanation_only_stays_direct(prompt):
    assert not needs_note([{"role": "user", "content": prompt}])


@pytest.mark.parametrize("compact", [False, True])
def test_config_guidance_distinguishes_runtime_settings_from_workspace(tmp_path, compact):
    builder = ContextBuilder(tmp_path)
    build = builder.build_compact_system_prompt if compact else builder.build_system_prompt
    prompt = build(memory_query="나노봇 설정 저장 방법을 설명해줘.")
    for required in ("--config", "~/.nanobot/config.json", "USER.md", "AGENTS.md", "SOUL.md",
                     "not those runtime settings", "without exposing credentials"):
        assert required in prompt


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("query", [None, "대한민국 수도는?", "기존 두 파일을 요약해줘.",
                                  "브라우저 설정 방법을 설명해줘."])
def test_config_facts_do_not_change_unrelated_context(tmp_path, compact, query):
    builder = ContextBuilder(tmp_path)
    build = builder.build_compact_system_prompt if compact else builder.build_system_prompt
    assert "not those runtime settings" not in build(memory_query=query)
