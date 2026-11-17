"""Selection is limited to explicitly optional notes, with complete recall fallback."""

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory_selection import select_memory
from nanobot.config.schema import Config
from tests.agent.test_compact_context import Script, call

MEMORY = """# Long-term Memory
## Preferences
- Reply in Korean. Never change gateway settings without separate approval.
## Reference Notes
### Aurora | 오로라
- Aurora code is K7V-42; the old value P1 is revoked.
### Borealis
- Borealis code is B8.
## Project Context
- Unclassified global constraint: preserve existing schedules.
"""


def select(query, text=MEMORY, **kwargs):
    return select_memory(text, query, source="/workspace/memory/MEMORY.md", **kwargs)


def test_named_fact_and_all_policies_preserved_without_source_mutation():
    selected = select("Aurora code")
    assert "Aurora code is K7V-42; the old value P1 is revoked." in selected
    assert "Borealis code is B8" not in selected
    assert "Never change gateway settings without separate approval." in selected
    assert "preserve existing schedules" in selected
    assert "1/2 notes" in selected
    assert "Omission is not absence" in selected
    assert "Full source: /workspace/memory/MEMORY.md" in selected
    assert "Borealis code is B8" in MEMORY


def test_korean_particle_matches_reference_heading():
    assert "K7V-42" in select("오로라의 식별코드를 알려줘")


def test_generic_verification_verb_does_not_select_unrelated_project():
    text = "# Memory\n## Reference Notes\n### Genie\n- 다음에 확인할 일: procedure status.\n### Aurora\n- A7Q-314.\n"
    assert "procedure status" not in select("a.md를 저장하고 다시 읽어 확인해줘", text)


def test_generic_query_shared_by_all_notes_keeps_complete_memory():
    assert select("code") == MEMORY


PATH_NOISE_MEMORY = """# Memory
## Rules
- Always keep the approval gate.
## Reference Notes
### Aurora | 오로라
- Aurora release code A7Q-314; local release status.
### Borealis | 보레알리스
- Borealis code B8; workspace artifacts.
"""


@pytest.mark.parametrize("path", [
    "/Users/test/Project/local/workspace/result.txt",
    r"C:\Users\test\local\workspace\result.txt",
    "local/workspace/result.txt",
    "local.txt",
    '"/Users/test/Local Files/workspace/result.txt"',
    r'"C:\Users\test\Local Files\workspace\result.txt"',
    "https://example.test/local/workspace/result.txt",
])
def test_file_paths_do_not_select_unrelated_reference_groups(path):
    result = select(f"운영 검증용 파일 {path}에 정확히 DEPLOYMENT_OK 문자열만 저장하고 다시 읽어 확인해줘. 다른 작업은 하지 마.",
                    PATH_NOISE_MEMORY)
    assert "Always keep the approval gate." in result
    assert "A7Q-314" not in result
    assert "Borealis code" not in result


def test_path_words_do_not_trigger_full_followup_fallback():
    result = select("파일 /tmp/previous/local/same/workspace/result.txt를 읽어줘.",
                    PATH_NOISE_MEMORY)
    assert "A7Q-314" not in result
    assert "Borealis code" not in result


def test_real_followup_and_path_only_queries_still_keep_full_memory():
    for query in ["그 파일 /tmp/local/workspace/result.txt를 다시 읽어줘.",
                  "/tmp/local/workspace/result.txt"]:
        assert select(query, PATH_NOISE_MEMORY) == PATH_NOISE_MEMORY


def test_explicit_identity_wins_over_incidental_body_words():
    result = select("오로라의 workspace artifacts를 설명해줘.", PATH_NOISE_MEMORY)
    assert "A7Q-314" in result
    assert "Borealis code" not in result
    assert "Always keep the approval gate." in result


def test_named_identity_outside_a_path_is_preserved():
    result = select("Aurora: /tmp/local/workspace/result.txt 파일을 읽어줘.",
                    PATH_NOISE_MEMORY)
    assert "A7Q-314" in result
    assert "Borealis code" not in result


def test_body_topic_matching_remains_available_without_a_project_name():
    result = select("workspace artifacts", PATH_NOISE_MEMORY)
    assert "Borealis code" in result
    assert "A7Q-314" not in result


def test_all_explicitly_named_groups_survive_a_small_budget():
    text = PATH_NOISE_MEMORY + "### Cygnus\n- Unrelated Cygnus observation.\n"
    result = select("Aurora와 보레알리스의 release code를 비교해줘.", text, budget=1)
    assert "A7Q-314" in result and "Borealis code" in result
    assert "Unrelated Cygnus" not in result


def test_explicit_alias_retains_cross_references_and_complete_corrections():
    text = (PATH_NOISE_MEMORY + "### Corrections\n"
            "- Aurora code A7Q-314 is now revoked; current value A7Q-315.\n"
            "### Cygnus\n- Unrelated Cygnus observation.\n")
    result = select("오로라의 현재 release code는?", text, budget=1)
    assert "A7Q-314" in result and "current value A7Q-315" in result
    assert "Borealis code" not in result and "Unrelated Cygnus" not in result


def test_longer_project_names_do_not_match_a_short_name_prefix():
    text = PATH_NOISE_MEMORY + "### Aurora-East\n- Eastern release code E9.\n"
    result = select("Aurora-East의 release code를 알려줘.", text)
    assert "Eastern release code E9" in result
    assert "A7Q-314" not in result


def test_dotted_explicit_project_name_is_not_lost_as_a_filename():
    text = PATH_NOISE_MEMORY + "### Aurora.One\n- Dotted project code D9.\n"
    result = select("Aurora.One의 release code를 알려줘.", text)
    assert "Dotted project code D9" in result
    assert "A7Q-314" not in result and "Borealis code" not in result


def test_filename_can_be_an_explicit_reference_group():
    text = PATH_NOISE_MEMORY + "### release.json\n- Deployment schema version 7.\n"
    result = select("release.json의 schema version은?", text)
    assert "Deployment schema version 7" in result
    assert "A7Q-314" not in result and "Borealis code" not in result


def test_sentence_punctuation_does_not_hide_an_explicit_name():
    result = select("workspace artifacts: Aurora.", PATH_NOISE_MEMORY)
    assert "A7Q-314" in result and "Borealis code" not in result


def test_cross_reference_at_sentence_end_is_kept_over_budget():
    text = (PATH_NOISE_MEMORY + "### Corrections\n"
            "- Current value A7Q-315. This correction pertains to Aurora.\n")
    result = select("오로라의 현재 release code는?", text, budget=1)
    assert "A7Q-314" in result and "Current value A7Q-315" in result
    assert "Borealis code" not in result


def test_search_term_cleanup_does_not_rewrite_the_actual_user_request(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory/MEMORY.md").write_text(PATH_NOISE_MEMORY)
    request = "파일 /tmp/local/workspace/result.txt를 읽어줘."
    builder = ContextBuilder(tmp_path, selective_memory=True)

    messages = builder.build_messages(history=[], current_message=request)

    assert request in messages[-1]["content"]
    assert "A7Q-314" not in messages[0]["content"]
    assert "Always keep the approval gate." in messages[0]["content"]


def test_dotted_technical_topic_is_not_automatically_treated_as_a_file():
    text = PATH_NOISE_MEMORY + "- Authentication uses shared.auth.\n"
    result = select("shared.auth가 뭐야?", text)
    assert "Authentication uses shared.auth." in result
    assert "A7Q-314" not in result


@pytest.mark.parametrize("query", ["", "네", "그 코드로 계속해줘", "same code", "기억 전체를 그대로 보여줘", "all my memory"])
def test_ambiguous_followup_or_full_recall_preserves_all(query):
    assert select(query) == MEMORY


def test_existing_mixed_project_context_is_not_automatically_classified():
    text = MEMORY.replace("## Reference Notes", "## Project Context")
    assert select("2+2", text) == text


@pytest.mark.parametrize("body", ["Always request approval.\n", "```python\npass\n```\n", "# Unexpected heading\n", "- Fact\nUnindented continuation\n"])
def test_unrecognized_reference_format_falls_back_to_full(body):
    text = "# Memory\n## Reference Notes\n" + body
    assert select("2+2", text) == text


def test_matching_large_note_is_not_truncated_to_fit_budget():
    note = "- Aurora " + "important detail " * 300 + "final revocation condition.\n"
    text = "# Memory\n## Reference Notes\n" + note + "- Borealis unrelated.\n"
    assert note in select("Aurora", text, budget=20)


def test_project_group_keeps_separate_correction_even_over_budget():
    text = ("# Memory\n## Reference Notes\n### Aurora\n"
            "- Aurora old code OLD-19. " + "detail " * 300 + "\n"
            "- Correction: OLD-19 is revoked; current code A7Q-314.\n"
            "### Borealis\n- Borealis code B8.\n")
    selected = select("Aurora", text, budget=20)
    assert "Correction: OLD-19 is revoked; current code A7Q-314." in selected
    assert "Borealis code" not in selected


def test_note_continuations_and_multiple_sections_are_complete():
    text = "# Memory\n## Reference Notes\n- Aurora is K7V.\n  Original qualifier: expires tomorrow.\n## Rules\nKEEP ALWAYS\n## 참고 메모\n- Borealis B8.\n"
    result = select("Aurora", text)
    assert "Original qualifier: expires tomorrow." in result
    assert "## Rules\nKEEP ALWAYS\n" in result
    assert "Borealis" not in result


def test_default_off_and_builders_retain_policy_summary_and_custom_skills(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory/MEMORY.md").write_text(MEMORY)
    (tmp_path / "AGENTS.md").write_text("Approve every external send.")
    custom = tmp_path / "skills/custom/SKILL.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("---\nname: custom\ndescription: policy\nalways: true\n---\nCUSTOM POLICY")
    assert not Config().agents.defaults.selective_memory
    assert Config.model_validate({"agents": {"defaults": {"selectiveMemory": True}}}).agents.defaults.selective_memory
    assert "Borealis code" in ContextBuilder(tmp_path).build_system_prompt(memory_query="Aurora")
    builder = ContextBuilder(tmp_path, selective_memory=True)
    for method in [builder.build_system_prompt, builder.build_compact_system_prompt]:
        prompt = method(memory_query="Aurora", session_summary="Earlier user constraint.")
        for required in ["Approve every external send", "CUSTOM POLICY", "Earlier user constraint", "K7V-42", "Never change gateway settings"]:
            assert required in prompt
        assert "Borealis code" not in prompt
        assert "Borealis code" in method()  # Calls without a task keep complete memory.
    other_project = tmp_path / "other-project"
    other_project.mkdir()
    assert "Borealis code" in builder.build_system_prompt(memory_query="Aurora", workspace=other_project)


def test_config_reaches_loop_and_disabled_file_access_retains_full_memory(tmp_path):
    from nanobot.agent.loop import AgentLoop

    config = Config.model_validate({"agents": {"defaults": {
        "workspace": str(tmp_path), "selectiveMemory": True}},
        "tools": {"file": {"enable": False}, "safeMode": True}})
    loop = AgentLoop.from_config(config, provider=Script([]))
    try:
        assert not loop.context.selective_memory
    finally:
        loop.stop()


def test_unlabelled_followup_with_history_keeps_optional_project_context(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory/MEMORY.md").write_text(MEMORY)
    builder = ContextBuilder(tmp_path, selective_memory=True)
    messages = builder.build_messages(
        history=[{"role": "user", "content": "Aurora setup"},
                 {"role": "assistant", "content": "Let's inspect the settings."}],
        current_message="설정값을 바꿔줘")
    assert "Borealis code" in messages[0]["content"]
    assert "Aurora code" in messages[0]["content"]


async def test_compact_activation_does_not_restore_omitted_notes(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.filesystem import ReadFileTool
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import ToolsConfig
    from nanobot.providers.base import LLMResponse

    (tmp_path / "memory").mkdir()
    (tmp_path / "memory/MEMORY.md").write_text(MEMORY)
    (tmp_path / "a.md").write_text("actual file content")
    provider = Script([call("activate_tools", tool_names=["read_file"]),
                       call("read_file", path="a.md"), LLMResponse(content="actual file content")])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     compact_context=True, selective_memory=True, mcp_servers={},
                     tools_config=ToolsConfig(safe_mode=True), unified_session=False)
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path, restrict_to_workspace=True))
    try:
        response = await loop.process_direct("a.md를 읽어줘", tools=registry)
        assert response.content == "actual file content"
        for request in provider.calls:
            system = request["messages"][0]["content"]
            assert "Borealis code" not in system
            assert "Never change gateway settings" in system
        assert "Tool Usage Notes" in provider.calls[1]["messages"][0]["content"]
    finally:
        loop.stop()
